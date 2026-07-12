## Decomposition Strategy

The request-submit slice is migrated from low-dependency cleanup methods toward the primary submit path. Each step removes the original method from `ProxyService`; the inherited method remains the only implementation. A typed protocol records the minimum service capabilities used by the slice.

Dynamic access to `app.modules.proxy.service` globals is not introduced. Pure helpers must have canonical module ownership before methods that consume them move, which keeps dependencies explicit and avoids a second hidden monolith.

## Compatibility

Callers continue invoking methods on `ProxyService`. Tests may continue importing shared helper names from `app.modules.proxy.service` while implementation modules import canonical owners directly.
