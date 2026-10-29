import { ComponentType, lazy } from "react";

/**
 * Drop-in replacement for React.lazy that self-heals "stale chunk" crashes.
 *
 * After a deploy, a tab still holding the old index.html will try to
 * lazy-load chunks by their old content-hashed filenames, which now 404.
 * The dynamic import rejects; instead of crashing into an error boundary we
 * do a one-time full reload to fetch the fresh (no-cache) index.html and the
 * new chunk map. A sessionStorage flag guards against a reload loop if the
 * reload doesn't fix it (e.g. a genuine network failure).
 *
 * Vite 3 has no `vite:preloadError` event (added in Vite 4), so we catch the
 * import rejection at the source — which also keeps working after an upgrade.
 */
export function lazyWithReload<T extends ComponentType<any>>(
  factory: () => Promise<{ default: T }>
) {
  return lazy(() =>
    factory().catch((err) => {
      if (!sessionStorage.getItem("chunk-reloaded")) {
        sessionStorage.setItem("chunk-reloaded", "1");
        window.location.reload();
        // Stall the import so Suspense keeps showing the fallback (no error
        // flash) until the reload navigates away.
        return new Promise<{ default: T }>(() => {});
      }
      throw err;
    })
  );
}
