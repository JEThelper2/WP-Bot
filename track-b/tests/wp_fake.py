"""A minimal in-memory WordPress REST API, shared by the B1 (client) and
B2 (allowlist) test suites."""

import json

import httpx

SITE = "https://wp.example.com"


def wp_post(post_id, title, content, status="publish", categories=(1,)):
    return {
        "id": post_id,
        "title": {"raw": title, "rendered": title},
        "content": {"raw": content, "rendered": content},
        "status": status,
        "categories": list(categories),
        "link": f"{SITE}/?p={post_id}",
    }


class FakeWordPress:
    """In-memory WordPress REST API; pass `.handler` to MockTransport."""

    # Capabilities implied by each role (used by the users/me probe).
    ROLE_CAPABILITIES = {
        "administrator": {"manage_options": True, "edit_posts": True},
        "editor": {"edit_posts": True},
        "author": {"edit_posts": True},
        "contributor": {},
        "subscriber": {},
    }

    def __init__(
        self,
        *,
        has_jobs_cpt=False,
        has_muplugin=True,
        user_roles=("editor",),
        expected_auth=None,
    ):
        self.posts: dict[int, dict] = {}
        self.next_id = 1
        self.categories = {"jobs": 10, "announcements": 11}
        self.option: dict = {}
        self.media: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.has_jobs_cpt = has_jobs_cpt
        self.has_muplugin = has_muplugin
        self.user_roles = list(user_roles)
        # (username, app_password) that authenticate; anything else -> 401.
        self.expected_auth = expected_auth
        self.inject_status: int | None = None
        self.inject_body: dict | None = None
        self.connect_error = False

    # -- handlers ---------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.inject_status:
            return httpx.Response(
                self.inject_status,
                json=self.inject_body or {"code": "injected", "message": "injected"},
            )
        if self.connect_error:
            raise httpx.ConnectError("connection refused", request=request)

        method = request.method
        path = request.url.path
        if path == "/wp-json/":
            return httpx.Response(
                200, json={"name": "WP-Bot Sandbox", "routes": {"/wp/v2": True}}
            )
        if path == "/wp-json/wp/v2/users/me":
            if self.expected_auth is not None:
                import base64

                expected = "Basic " + base64.b64encode(
                    f"{self.expected_auth[0]}:{self.expected_auth[1]}".encode()
                ).decode()
                if request.headers.get("authorization") != expected:
                    return httpx.Response(
                        401,
                        json={"code": "rest_forbidden", "message": "Sorry, you are not allowed to do that."},
                    )
            caps = self.ROLE_CAPABILITIES.get(self.user_roles[-1], {})
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "name": self.user_roles[-1],
                    "roles": self.user_roles,
                    "capabilities": caps,
                },
            )
        if path == "/wp-json/wp/v2/types":
            types = {"post": {"rest_base": "posts"}}
            if self.has_jobs_cpt:
                types["jobs"] = {"rest_base": "jobs"}
            return httpx.Response(200, json=types)
        if path == "/wp-json/wp/v2/categories":
            if method == "GET":
                name = request.url.params.get("search", "").lower()
                cats = [
                    {"id": cid, "name": cname}
                    for cname, cid in self.categories.items()
                    if cname == name
                ]
                return httpx.Response(200, json=cats)
            body = json.loads(request.content)
            name = body["name"]
            if name not in self.categories:
                self.categories[name] = max(self.categories.values()) + 1
            return httpx.Response(200, json={"id": self.categories[name], "name": name})
        if path == "/wp-json/wpbot/v1/business-info":
            if not self.has_muplugin:
                return httpx.Response(404, json={"code": "rest_no_route", "message": "No route"})
            if method == "GET":
                return httpx.Response(200, json={"value": self.option})
            body = json.loads(request.content)
            for key, value in body.get("fields", {}).items():
                self.option[key] = value
            return httpx.Response(200, json={"value": self.option})
        if path == "/wp-json/wp/v2/media":
            attachment = {
                "id": len(self.media) + 1,
                "source_url": f"{SITE}/wp-content/uploads/{len(self.media) + 1}.jpg",
            }
            self.media.append(attachment)
            return httpx.Response(201, json=attachment)

        # posts / jobs CRUD
        segments = [s for s in path.split("/") if s]
        if segments[-1] in ("posts", "jobs"):
            post_type = segments[-1]
            collection = True
        elif len(segments) >= 5 and segments[-2] in ("posts", "jobs"):
            post_type = segments[-2]
            collection = False
        else:
            return httpx.Response(404, json={"code": "rest_no_route", "message": "No route"})

        if collection and method == "GET":
            # list/search: find_post_by_title
            search = request.url.params.get("search", "").lower()
            if search:
                matches = [
                    p for p in self.posts.values()
                    if search in (p["title"]["raw"] or "").lower()
                ]
                return httpx.Response(200, json=matches)
            return httpx.Response(200, json=list(self.posts.values()))
        if method == "POST":
            body = json.loads(request.content)
            post_id = self.next_id
            self.next_id += 1
            post = wp_post(
                post_id,
                body.get("title", ""),
                body.get("content", ""),
                body.get("status", "publish"),
                body.get("categories", []),
            )
            self.posts[post_id] = post
            return httpx.Response(201, json=post)
        if collection:
            return httpx.Response(404, json={"code": "rest_post_invalid_id", "message": "Invalid post ID."})
        post_id = int(segments[-1])
        post = self.posts.get(post_id)
        if post is None:
            return httpx.Response(404, json={"code": "rest_post_invalid_id", "message": "Invalid post ID."})
        if method == "GET":
            return httpx.Response(200, json=post)
        if method == "PUT":
            body = json.loads(request.content)
            for key, value in body.items():
                if key == "title":
                    post["title"] = {"raw": value, "rendered": value}
                elif key == "content":
                    post["content"] = {"raw": value, "rendered": value}
                else:
                    post[key] = value
            return httpx.Response(200, json=post)
        if method == "DELETE":
            post["status"] = "trash"
            return httpx.Response(200, json=post)
        return httpx.Response(405, json={"code": "rest_no_route", "message": "No route"})
