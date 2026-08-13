#!/usr/bin/env perl
use strict;
use warnings;

use Digest::SHA qw(sha256_hex);
use Encode qw(decode encode FB_CROAK);
use File::Spec;
use JSON::PP qw(decode_json);
use Scalar::Util qw(looks_like_number);

sub env_text {
    my ($name, $default) = @_;
    return $default unless exists $ENV{$name};

    my $value = $ENV{$name};
    return $value if utf8::is_utf8($value);
    my $decoded = eval { decode('UTF-8', $value, FB_CROAK) };
    return defined $decoded ? $decoded : $value;
}

sub cache_fresh {
    my ($path, $ttl) = @_;
    return 0 unless -f $path;

    my @stat = stat($path);
    return -1 unless @stat;
    return 1 if $ttl < 0;
    return time() - $stat[9] <= $ttl ? 1 : 0;
}

sub route_status {
    my ($raw_payload) = @_;
    return 2 unless defined $raw_payload && length($raw_payload) <= 1024 * 1024;
    my $payload = eval { decode_json($raw_payload) };
    return 2 unless ref($payload) eq 'HASH';

    my $prompt = $payload->{prompt};
    my $session_id = $payload->{session_id};
    return 1 unless defined $prompt && !ref($prompt) && length($prompt);
    return 0 if $prompt eq '/compact';
    return 2 unless defined $session_id && !ref($session_id);

    my $repo_root = env_text('MNEMOS_REPO_ROOT', '');
    return 2 unless length($repo_root);
    my $cache_dir = env_text('MNEMOS_CONTEXT_CACHE_DIR', '');
    if (!length($cache_dir)) {
        my $tmp_dir = env_text('TMPDIR', '/tmp');
        $cache_dir = File::Spec->catdir($tmp_dir, 'mnemos-context-cache');
    }

    my $ttl_raw = env_text('MNEMOS_CONTEXT_CACHE_TTL_SECONDS', '300');
    return 2 unless $ttl_raw =~ /\A[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\z/;
    my $ttl = int($ttl_raw + 0);
    my $override = env_text('MNEMOS_CONTEXT_CACHE_FILE', '');
    my $exact_key = sha256_hex(
        encode('UTF-8', join("\0", $repo_root, $session_id, $prompt))
    );
    my $session_key = sha256_hex(
        encode('UTF-8', join("\0", $repo_root, $session_id))
    );
    my $exact_path = length($override)
        ? $override
        : File::Spec->catfile($cache_dir, 'exact', "$exact_key.txt");
    my $session_path = File::Spec->catfile(
        $cache_dir,
        'session',
        "$session_key.txt",
    );

    for my $cache_path ($exact_path, $session_path) {
        my $fresh = cache_fresh($cache_path, $ttl);
        return 2 if $fresh < 0;
        return 0 if $fresh;
    }

    my $observability_log = File::Spec->catfile(
        $repo_root,
        '.agent',
        'observability.jsonl',
    );
    return 1 unless -e $observability_log;
    my @log_stat = stat($observability_log);
    return 2 unless @log_stat;

    my $cursor_path = env_text('MNEMOS_PROMO_CURSOR', '');
    if (!length($cursor_path)) {
        my $home = env_text('HOME', '');
        return 2 unless length($home);
        $cursor_path = File::Spec->catfile(
            $home,
            '.mnemos',
            '.cache',
            'promotion-cursor.txt',
        );
    }
    return 0 unless -f $cursor_path;
    open my $cursor_fh, '<:raw', $cursor_path or return 2;
    my $cursor_raw = do { local $/; <$cursor_fh> };
    close $cursor_fh;
    return 2 unless defined $cursor_raw && length($cursor_raw) <= 65536;

    my $cursor = eval { decode_json($cursor_raw) };
    return 0 unless ref($cursor) eq 'HASH';
    my $cursor_inode = $cursor->{inode};
    my $cursor_offset = $cursor->{offset};
    return 0 unless defined $cursor_inode && looks_like_number($cursor_inode);
    return 0 unless defined $cursor_offset && looks_like_number($cursor_offset);
    return 1 if int($cursor_inode) == $log_stat[1]
        && int($cursor_offset) == $log_stat[7];

    return 0;
}

unless (caller) {
    my $raw_payload = do { local $/; <STDIN> };
    exit route_status($raw_payload);
}

1;
