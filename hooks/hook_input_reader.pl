#!/usr/bin/env perl
use strict;
use warnings;

use Errno qw(EAGAIN EWOULDBLOCK EINTR);
use Fcntl qw(F_GETFL F_SETFL O_NONBLOCK);
use FindBin;
use IO::Select;
use Time::HiRes qw(time);

my $max_bytes = 1024 * 1024;
my $deadline = time() + 3.0;
my $payload = '';
my @stack;
my $started = 0;
my $closed = 0;
my $in_string = 0;
my $escaped = 0;
my $route = @ARGV && $ARGV[0] eq '--route';

my $flags = fcntl(STDIN, F_GETFL, 0);
fcntl(STDIN, F_SETFL, $flags | O_NONBLOCK) if defined $flags;
my $selector = IO::Select->new(\*STDIN);

while (length($payload) < $max_bytes) {
    my $remaining = $deadline - time();
    last if $remaining <= 0;

    my @ready = $selector->can_read($remaining < 0.05 ? $remaining : 0.05);
    next unless @ready;

    my $chunk = '';
    my $read = sysread(STDIN, $chunk, $max_bytes - length($payload));
    if (!defined $read) {
        next if $! == EAGAIN || $! == EWOULDBLOCK || $! == EINTR;
        exit 1;
    }
    last if $read == 0;

    $payload .= $chunk;
    for my $char (split //, $chunk) {
        if ($closed) {
            exit 1 unless $char =~ /[\x20\t\r\n]/;
            next;
        }
        if ($in_string) {
            if ($escaped) {
                $escaped = 0;
            } elsif ($char eq '\\') {
                $escaped = 1;
            } elsif ($char eq '"') {
                $in_string = 0;
            } elsif ($char eq "\n" || $char eq "\r") {
                exit 1;
            }
            next;
        }
        if ($char eq '"') {
            $in_string = 1;
        } elsif ($char eq '{' || $char eq '[') {
            $started = 1;
            push @stack, $char;
        } elsif ($char eq '}' || $char eq ']') {
            my $expected = $char eq '}' ? '{' : '[';
            exit 1 unless @stack && $stack[-1] eq $expected;
            pop @stack;
            $closed = 1 unless @stack;
        } elsif (!$started && $char !~ /[\x20\t\r\n]/) {
            exit 1;
        }
    }

    if ($started && $closed && !$in_string && !@stack) {
        if ($route) {
            require "$FindBin::Bin/hook_route.pl";
            my $status = route_status($payload);
            print STDOUT "$status\n";
        }
        print STDOUT $payload;
        exit 0;
    }
}

exit 1;
