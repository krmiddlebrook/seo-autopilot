<?php
/**
 * Plugin Name: SEO Autopilot Receiver
 * Description: Receives articles from the local seo-autopilot app (Soro-compatible API). Sets SEO title/description for Yoast, Rank Math and AIOSEO, featured image with alt text, JSON-LD schema, and serves the IndexNow key file.
 * Version: 0.1.0
 * Requires PHP: 7.4
 * License: MIT
 */

if (!defined('ABSPATH')) {
    exit;
}

const AUTOPILOT_OPT_KEY = 'autopilot_receiver_api_key';
const AUTOPILOT_OPT_INDEXNOW = 'autopilot_receiver_indexnow_key';
const AUTOPILOT_META_ID = 'soro_article_id';
const AUTOPILOT_META_SCHEMA = '_autopilot_schema';

register_activation_hook(__FILE__, function () {
    if (!get_option(AUTOPILOT_OPT_KEY)) {
        update_option(AUTOPILOT_OPT_KEY, wp_generate_password(48, false, false), false);
    }
});

/* ---------------------------------------------------------------- auth */

function autopilot_request_key(WP_REST_Request $request): string
{
    $auth = (string) $request->get_header('authorization');
    if (stripos($auth, 'bearer ') === 0) {
        return trim(substr($auth, 7));
    }
    foreach (['x_autopilot_key', 'x_soro_key'] as $h) {
        $v = (string) $request->get_header($h);
        if ($v !== '') {
            return trim($v);
        }
    }
    return '';
}

function autopilot_authorized(WP_REST_Request $request)
{
    $expected = (string) get_option(AUTOPILOT_OPT_KEY);
    $given = autopilot_request_key($request);
    if ($expected === '' || $given === '' || !hash_equals($expected, $given)) {
        return new WP_Error('autopilot_unauthorized', 'Invalid API key', ['status' => 401]);
    }
    return true;
}

// Security plugins sometimes block unauthenticated REST calls; let our own key-checked routes through.
add_filter('rest_authentication_errors', function ($result) {
    $uri = isset($_SERVER['REQUEST_URI']) ? (string) $_SERVER['REQUEST_URI'] : '';
    if (preg_match('#/(autopilot|soro)/v1/#', $uri)) {
        return null;
    }
    return $result;
}, 999);

/* -------------------------------------------------------------- routes */

add_action('rest_api_init', function () {
    foreach (['autopilot/v1', 'soro/v1'] as $ns) {
        register_rest_route($ns, '/verify', [
            'methods' => 'GET',
            'callback' => 'autopilot_verify',
            'permission_callback' => 'autopilot_authorized',
        ]);
        register_rest_route($ns, '/publish', [
            'methods' => 'POST',
            'callback' => 'autopilot_publish',
            'permission_callback' => 'autopilot_authorized',
        ]);
        register_rest_route($ns, '/content', [
            'methods' => 'GET',
            'callback' => 'autopilot_content',
            'permission_callback' => 'autopilot_authorized',
        ]);
        register_rest_route($ns, '/update', [
            'methods' => 'POST',
            'callback' => 'autopilot_update',
            'permission_callback' => 'autopilot_authorized',
        ]);
        register_rest_route($ns, '/setup-indexnow', [
            'methods' => 'POST',
            'callback' => 'autopilot_setup_indexnow',
            'permission_callback' => 'autopilot_authorized',
        ]);
        register_rest_route($ns, '/indexnow-status', [
            'methods' => 'GET',
            'callback' => 'autopilot_indexnow_status',
            'permission_callback' => 'autopilot_authorized',
        ]);
    }
});

function autopilot_seo_plugin(): string
{
    if (defined('WPSEO_VERSION')) {
        return 'yoast';
    }
    if (defined('RANK_MATH_VERSION')) {
        return 'rankmath';
    }
    if (defined('AIOSEO_VERSION')) {
        return 'aioseo';
    }
    return 'none';
}

function autopilot_verify(): WP_REST_Response
{
    return new WP_REST_Response([
        'ok' => true,
        'site' => get_bloginfo('name'),
        'url' => home_url('/'),
        'wordpress' => get_bloginfo('version'),
        'plugin' => '0.1.0',
        'seo_plugin' => autopilot_seo_plugin(),
        'indexnow' => (bool) get_option(AUTOPILOT_OPT_INDEXNOW),
    ]);
}

function autopilot_post_response(int $post_id, array $extra = []): WP_REST_Response
{
    $post = get_post($post_id);
    return new WP_REST_Response(array_merge([
        'success' => true,
        'post' => [
            'post_id' => $post_id,
            'url' => get_permalink($post_id),
            'status' => $post ? $post->post_status : '',
            'slug' => $post ? $post->post_name : '',
        ],
        'duplicate' => false,
        'slug_conflict' => false,
    ], $extra));
}

function autopilot_publish(WP_REST_Request $request)
{
    $p = $request->get_json_params();
    if (!is_array($p) || empty($p['title']) || empty($p['content'])) {
        return new WP_Error('autopilot_bad_request', 'title and content are required', ['status' => 400]);
    }
    $article_id = sanitize_text_field($p['soro_article_id'] ?? '');

    // Idempotency: the same article id never creates a second post.
    if ($article_id !== '') {
        $found = get_posts([
            'post_type' => 'post',
            'post_status' => 'any',
            'meta_key' => AUTOPILOT_META_ID,
            'meta_value' => $article_id,
            'fields' => 'ids',
            'numberposts' => 1,
        ]);
        if ($found) {
            return autopilot_post_response((int) $found[0], ['duplicate' => true]);
        }
    }

    $status = in_array($p['status'] ?? 'draft', ['draft', 'publish', 'pending', 'private', 'future'], true) ? $p['status'] : 'draft';
    $slug = sanitize_title($p['slug'] ?? $p['title']);
    $slug_conflict = false;
    if ($slug && get_page_by_path($slug, OBJECT, 'post')) {
        // Never take over a slug an existing post owns: save as a draft for a human to look at.
        $slug_conflict = true;
        $status = 'draft';
        $slug = $slug . '-' . substr(md5($article_id ?: wp_generate_password(8, false)), 0, 6);
    }

    $postarr = [
        'post_title' => wp_strip_all_tags($p['title']),
        'post_content' => wp_kses_post($p['content']),
        'post_excerpt' => sanitize_textarea_field($p['excerpt'] ?? ''),
        'post_status' => $status,
        'post_name' => $slug,
        'post_type' => 'post',
    ];
    if (!empty($p['author_id'])) {
        $postarr['post_author'] = (int) $p['author_id'];
    } else {
        $admins = get_users(['role' => 'administrator', 'number' => 1, 'fields' => 'ID']);
        if ($admins) {
            $postarr['post_author'] = (int) $admins[0];
        }
    }
    if (!empty($p['category'])) {
        $cat = $p['category'];
        $term = is_numeric($cat) ? get_term((int) $cat, 'category') : get_term_by('name', $cat, 'category');
        if (!$term || is_wp_error($term)) {
            $created = wp_insert_term(sanitize_text_field($cat), 'category');
            $term_id = is_wp_error($created) ? 0 : (int) $created['term_id'];
        } else {
            $term_id = (int) $term->term_id;
        }
        if ($term_id) {
            $postarr['post_category'] = [$term_id];
        }
    }

    $post_id = wp_insert_post(wp_slash($postarr), true);
    if (is_wp_error($post_id)) {
        return new WP_Error('autopilot_insert_failed', $post_id->get_error_message(), ['status' => 500]);
    }

    if ($article_id !== '') {
        update_post_meta($post_id, AUTOPILOT_META_ID, $article_id);
    }
    autopilot_set_seo_meta(
        $post_id,
        sanitize_text_field($p['meta_title'] ?? ''),
        sanitize_textarea_field($p['meta_description'] ?? ''),
        sanitize_text_field($p['focus_keyword'] ?? '')
    );
    if (!empty($p['schema'])) {
        update_post_meta($post_id, AUTOPILOT_META_SCHEMA, wp_slash(wp_json_encode($p['schema'])));
    }

    $image_error = '';
    $alt = sanitize_text_field($p['image_alt'] ?? $p['title']);
    if (!empty($p['featured_image_base64'])) {
        $image_error = autopilot_image_from_base64($post_id, $p['featured_image_base64'], $p['featured_image_filename'] ?? 'featured.png', $alt);
    } elseif (!empty($p['featured_image_url'])) {
        $image_error = autopilot_image_from_url($post_id, esc_url_raw($p['featured_image_url']), $alt);
    }

    if (autopilot_seo_plugin() === 'yoast' && function_exists('YoastSEO')) {
        try {
            $repo = YoastSEO()->classes->get('Yoast\WP\SEO\Repositories\Indexable_Repository');
            $indexable = $repo->find_by_id_and_type($post_id, 'post');
            if ($indexable) {
                $indexable->title = get_post_meta($post_id, '_yoast_wpseo_title', true) ?: null;
                $indexable->description = get_post_meta($post_id, '_yoast_wpseo_metadesc', true) ?: null;
                $indexable->save();
            }
        } catch (Throwable $e) {
            // Yoast rebuilds the indexable on its own schedule; this just makes it immediate.
        }
    }

    return autopilot_post_response((int) $post_id, ['slug_conflict' => $slug_conflict, 'image_error' => $image_error]);
}

/* ------------------------------------------------------ refresh existing */

function autopilot_seo_value(int $post_id, string $which): string
{
    $keys = $which === 'title'
        ? ['_yoast_wpseo_title', 'rank_math_title', '_aioseo_title']
        : ['_yoast_wpseo_metadesc', 'rank_math_description', '_aioseo_description'];
    foreach ($keys as $key) {
        $v = (string) get_post_meta($post_id, $key, true);
        if ($v !== '') {
            return $v;
        }
    }
    return '';
}

function autopilot_content(WP_REST_Request $request)
{
    $url = (string) $request->get_param('url');
    $post_id = $url !== '' ? url_to_postid($url) : (int) $request->get_param('post_id');
    $post = $post_id ? get_post($post_id) : null;
    if (!$post) {
        return new WP_Error('autopilot_not_found', 'No post at that URL', ['status' => 404]);
    }
    return new WP_REST_Response([
        'post_id' => $post->ID,
        'url' => get_permalink($post->ID),
        'title' => $post->post_title,
        'content' => $post->post_content,
        'status' => $post->post_status,
        'modified' => $post->post_modified_gmt,
        'meta_title' => autopilot_seo_value($post->ID, 'title'),
        'meta_description' => autopilot_seo_value($post->ID, 'description') ?: $post->post_excerpt,
    ]);
}

function autopilot_update(WP_REST_Request $request)
{
    $p = $request->get_json_params();
    $post_id = is_array($p) ? (int) ($p['post_id'] ?? 0) : 0;
    $post = $post_id ? get_post($post_id) : null;
    if (!$post) {
        return new WP_Error('autopilot_not_found', 'No such post', ['status' => 404]);
    }
    // Refuse to overwrite an edit made after the refresh was planned.
    if (!empty($p['expected_modified']) && $p['expected_modified'] !== $post->post_modified_gmt) {
        return new WP_Error('autopilot_conflict', 'The post changed since the refresh was planned', ['status' => 409]);
    }
    if (isset($p['content']) && $p['content'] !== $post->post_content) {
        $res = wp_update_post(['ID' => $post_id, 'post_content' => wp_kses_post($p['content'])], true);
        if (is_wp_error($res)) {
            return $res;
        }
    }
    autopilot_set_seo_meta($post_id, sanitize_text_field($p['meta_title'] ?? ''),
        sanitize_text_field($p['meta_description'] ?? ''), '');
    return autopilot_post_response($post_id);
}

function autopilot_set_seo_meta(int $post_id, string $title, string $desc, string $keyword): void
{
    if ($title !== '') {
        update_post_meta($post_id, '_yoast_wpseo_title', $title);
        update_post_meta($post_id, 'rank_math_title', $title);
        update_post_meta($post_id, '_aioseo_title', $title);
    }
    if ($desc !== '') {
        update_post_meta($post_id, '_yoast_wpseo_metadesc', $desc);
        update_post_meta($post_id, 'rank_math_description', $desc);
        update_post_meta($post_id, '_aioseo_description', $desc);
    }
    if ($keyword !== '') {
        update_post_meta($post_id, '_yoast_wpseo_focuskw', $keyword);
        update_post_meta($post_id, 'rank_math_focus_keyword', $keyword);
    }
    // AIOSEO v4 reads its own table, not post meta.
    if (($title !== '' || $desc !== '') && class_exists('\AIOSEO\Plugin\Common\Models\Post')) {
        try {
            $row = \AIOSEO\Plugin\Common\Models\Post::getPost($post_id);
            $row->post_id = $post_id;
            if ($title !== '') {
                $row->title = $title;
            }
            if ($desc !== '') {
                $row->description = $desc;
            }
            if ($keyword !== '') {
                $row->keyphrases = wp_json_encode(['focus' => ['keyphrase' => $keyword]]);
            }
            $row->save();
        } catch (Throwable $e) {
            // post meta above is still in place
        }
    }
}

function autopilot_attach_file(int $post_id, string $tmp, string $filename, string $alt): string
{
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';
    require_once ABSPATH . 'wp-admin/includes/image.php';
    $file = ['name' => sanitize_file_name($filename), 'tmp_name' => $tmp];
    $attachment_id = media_handle_sideload($file, $post_id, get_the_title($post_id));
    if (is_wp_error($attachment_id)) {
        @unlink($tmp);
        return $attachment_id->get_error_message();
    }
    update_post_meta($attachment_id, '_wp_attachment_image_alt', $alt);
    set_post_thumbnail($post_id, $attachment_id);
    return '';
}

function autopilot_image_from_url(int $post_id, string $url, string $alt): string
{
    require_once ABSPATH . 'wp-admin/includes/file.php';
    $tmp = download_url($url, 60);
    if (is_wp_error($tmp)) {
        return $tmp->get_error_message();
    }
    $name = basename(parse_url($url, PHP_URL_PATH) ?: 'featured.jpg');
    if (!preg_match('/\.(jpe?g|png|gif|webp)$/i', $name)) {
        $name .= '.jpg';
    }
    return autopilot_attach_file($post_id, $tmp, $name, $alt);
}

function autopilot_image_from_base64(int $post_id, string $data, string $filename, string $alt): string
{
    $bytes = base64_decode($data, true);
    if ($bytes === false || strlen($bytes) > 15 * 1024 * 1024) {
        return 'invalid or oversized image data';
    }
    $info = @getimagesizefromstring($bytes);
    if (!$info || !in_array($info['mime'], ['image/jpeg', 'image/png', 'image/gif', 'image/webp'], true)) {
        return 'not a supported image';
    }
    $tmp = wp_tempnam($filename);
    file_put_contents($tmp, $bytes);
    return autopilot_attach_file($post_id, $tmp, $filename, $alt);
}

/* ------------------------------------------------------------- schema */

add_action('wp_head', function () {
    if (!is_singular('post')) {
        return;
    }
    $json = get_post_meta(get_the_ID(), AUTOPILOT_META_SCHEMA, true);
    if (!$json) {
        return;
    }
    $data = json_decode($json, true);
    if (!is_array($data)) {
        return;
    }
    foreach (isset($data['@type']) ? [$data] : $data as $node) {
        echo '<script type="application/ld+json">' . wp_json_encode($node, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) . "</script>\n";
    }
}, 20);

/* ----------------------------------------------------------- IndexNow */

function autopilot_setup_indexnow(): WP_REST_Response
{
    $key = get_option(AUTOPILOT_OPT_INDEXNOW);
    if (!$key) {
        $key = bin2hex(random_bytes(16));
        update_option(AUTOPILOT_OPT_INDEXNOW, $key, false);
    }
    return new WP_REST_Response(['success' => true, 'key' => $key, 'key_location' => home_url('/' . $key . '.txt')]);
}

function autopilot_indexnow_status(): WP_REST_Response
{
    $key = get_option(AUTOPILOT_OPT_INDEXNOW);
    return new WP_REST_Response([
        'configured' => (bool) $key,
        'key' => $key ?: '',
        'key_location' => $key ? home_url('/' . $key . '.txt') : '',
    ]);
}

// Serve /{key}.txt without touching the filesystem.
add_action('parse_request', function () {
    $key = get_option(AUTOPILOT_OPT_INDEXNOW);
    if (!$key) {
        return;
    }
    $path = trim((string) parse_url($_SERVER['REQUEST_URI'] ?? '', PHP_URL_PATH), '/');
    if ($path === $key . '.txt') {
        header('Content-Type: text/plain; charset=utf-8');
        echo $key;
        exit;
    }
}, 0);

/* ----------------------------------------------------------- settings */

add_action('admin_menu', function () {
    add_options_page('SEO Autopilot', 'SEO Autopilot', 'manage_options', 'autopilot-receiver', 'autopilot_settings_page');
});

function autopilot_settings_page(): void
{
    if (!current_user_can('manage_options')) {
        return;
    }
    if (isset($_POST['autopilot_regenerate']) && check_admin_referer('autopilot_regenerate')) {
        update_option(AUTOPILOT_OPT_KEY, wp_generate_password(48, false, false), false);
        echo '<div class="notice notice-success"><p>New API key generated. Update WP_RECEIVER_KEY in your .env.</p></div>';
    }
    $key = (string) get_option(AUTOPILOT_OPT_KEY);
    $indexnow = (string) get_option(AUTOPILOT_OPT_INDEXNOW);
    ?>
    <div class="wrap">
        <h1>SEO Autopilot Receiver</h1>
        <p>Put this key in the <code>.env</code> file next to <code>autopilot.toml</code> as <code>WP_RECEIVER_KEY</code>.</p>
        <table class="form-table">
            <tr><th>API key</th><td><input type="text" readonly class="regular-text code" value="<?php echo esc_attr($key); ?>" onclick="this.select()"></td></tr>
            <tr><th>Endpoint</th><td><code><?php echo esc_html(rest_url('autopilot/v1/publish')); ?></code></td></tr>
            <tr><th>SEO plugin</th><td><?php echo esc_html(autopilot_seo_plugin()); ?></td></tr>
            <tr><th>IndexNow</th><td><?php echo $indexnow ? '<a href="' . esc_url(home_url('/' . $indexnow . '.txt')) . '">key file live</a>' : 'not set up (run <code>autopilot indexnow setup</code>)'; ?></td></tr>
        </table>
        <form method="post">
            <?php wp_nonce_field('autopilot_regenerate'); ?>
            <p><button class="button" name="autopilot_regenerate" value="1" onclick="return confirm('The old key stops working immediately. Continue?')">Regenerate key</button></p>
        </form>
    </div>
    <?php
}
