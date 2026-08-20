<?php
/**
 * WP-Bot: Allow Application Passwords over HTTP for local development.
 *
 * In production, Application Passwords require HTTPS. This mu-plugin
 * disables that check for the local sandbox so integration tests can
 * authenticate over plain HTTP.
 */
add_filter( 'wp_is_application_passwords_available', '__return_true' );
add_filter( 'wp_is_application_passwords_available_for_user', '__return_true' );
add_filter( 'application_password_is_api_request', '__return_true' );
