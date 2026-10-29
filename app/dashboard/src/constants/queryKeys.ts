/**
 * React Query cache keys shared across hooks and components.
 *
 * These lived in `components/Statistics.tsx`, which meant any hook that needed
 * to invalidate a statistics card had to import that module — dragging the
 * whole Chakra card tree, its icons and DashboardContext in behind three string
 * constants, and pointing a hooks module at a component. They are declared here
 * instead; `components/Statistics.tsx` re-exports them so the existing import
 * sites keep working.
 */
export const StatisticsQueryKey = "statistics-query-key";
export const DeviceStatsQueryKey = "device-stats-query-key";
export const TopUsersQueryKey = "top-users";
export const ClientAppsQueryKey = "client-apps";
