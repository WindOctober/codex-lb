# Change: Hide disabled News and Scholar dashboard pages

## Why

News and Scholar are optional dashboard surfaces backed by external refresh jobs. Proxy-only deployments often disable `news_refresh_enabled` and `scholar_refresh_enabled`, but the React shell still shows the News and Scholar navigation entries and pages.

## What Changes

- Expose read-only News and Scholar refresh feature flags through the dashboard settings response.
- Hide News and Scholar navigation entries when their corresponding refresh flag is disabled.
- Redirect direct visits to disabled News or Scholar pages back to the dashboard.

## Out of Scope

- Changing the News or Scholar refresh services.
- Adding UI controls to mutate these environment-backed flags.
