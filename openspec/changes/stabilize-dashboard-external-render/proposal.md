# stabilize-dashboard-external-render

## Summary

Prevent externally forwarded dashboard sessions from rendering as a blank page when browser startup APIs or client rendering fail.

## Motivation

LAN/forwarded dashboard access can run in browser contexts with stricter storage settings, missing legacy media-query APIs, or cached static assets. A startup exception currently leaves only the page background visible, which makes the failure hard to diagnose.

## Approach

- Make theme startup tolerant of blocked storage and older media-query listener APIs.
- Add a visible dashboard render fallback for boot/render errors.
- Make SPA HTML and static asset cache behavior explicit.
