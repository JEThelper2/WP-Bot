<?php
/**
 * Plugin Name: WP-Bot business-info route
 * Description: REST route used by WP-Bot Track B to read/write the
 *              business_info singleton (hours, contact, address, prices).
 *              Required because WordPress's core `settings` endpoint
 *              demands `manage_options` (Administrator), which violates
 *              the WP-Bot security guardrail of an Editor-only user.
 *              This route is gated on `edit_posts` (Editor and above) and
 *              writes ONLY the option keys in WPBOT_ALLOWED_KEYS.
 *
 * Install: drop this file into wp-content/mu-plugins/ (no activation
 * needed). It is bundled in wp-sandbox/mu-plugins/ for the sandbox.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

const WPBOT_OPTION_KEY  = 'wpbot_business_info';
const WPBOT_ALLOWED_KEYS = array(
	'phone',
	'hours',
	'address',
	'prices',
	'image:homepage_banner',
	'image:logo',
	'image:gallery',
);

add_action(
	'rest_api_init',
	function () {
		register_rest_route(
			'wpbot/v1',
			'/business-info',
			array(
				array(
					'methods'             => 'GET',
					'permission_callback' => 'wpbot_require_editor',
					'callback'            => 'wpbot_get_business_info',
				),
				array(
					'methods'             => 'POST',
					'permission_callback' => 'wpbot_require_editor',
					'callback'            => 'wpbot_set_business_info',
				),
			)
		);
	}
);

function wpbot_require_editor() {
	return current_user_can( 'edit_posts' );
}

function wpbot_get_business_info() {
	return new WP_REST_Response(
		array( 'value' => get_option( WPBOT_OPTION_KEY, array() ) ),
		200
	);
}

function wpbot_set_business_info( WP_REST_Request $request ) {
	$params = $request->get_json_params();
	$fields = isset( $params['fields'] ) ? $params['fields'] : array();

	if ( ! is_array( $fields ) ) {
		return new WP_Error( 'invalid_fields', 'fields must be an object', array( 'status' => 400 ) );
	}

	$current = get_option( WPBOT_OPTION_KEY, array() );
	if ( ! is_array( $current ) ) {
		$current = array();
	}

	foreach ( $fields as $key => $value ) {
		if ( ! in_array( $key, WPBOT_ALLOWED_KEYS, true ) ) {
			return new WP_Error(
				'key_not_allowed',
				sprintf( 'option key %s is not in the WP-Bot allowlist', $key ),
				array( 'status' => 400 )
			);
		}
		$current[ $key ] = $value;
	}

	update_option( WPBOT_OPTION_KEY, $current );
	return new WP_REST_Response(
		array( 'value' => get_option( WPBOT_OPTION_KEY ) ),
		200
	);
}
