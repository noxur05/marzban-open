import {
  Box,
  Flex,
  HStack,
  SimpleGrid,
  Skeleton,
  Tab,
  TabList,
  TabPanel,
  TabPanels,
  Tabs,
  Text,
  useBreakpointValue,
  useColorMode,
  useToken,
  VStack,
} from "@chakra-ui/react";
import { ApexOptions } from "apexcharts";
import { ClientApps } from "components/ClientApps";
import { AdminsRanking } from "components/stats/AdminsRanking";
import { FilterBar, Preset } from "components/stats/FilterBar";
import { TopUsers } from "components/TopUsers";
import {
  Delta,
  EmptyState,
  LoadError,
  Page,
  PageHeader,
  SegmentedControl,
  StatTile,
  StickyBar,
} from "components/shared";
import { useAdminsUsage, usePreviousUsage } from "hooks/useAdminsUsage";
import { useClientApps } from "hooks/useClientApps";
import { useTopUsers } from "hooks/useTopUsers";
import dayjs from "dayjs";
import { FC, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import { formatBytes, numberWithCommas } from "utils/formatByte";
import { ashgabatToday } from "utils/dates";
import { lazyWithReload } from "utils/lazyWithReload";
import {
  buildUsageChart,
  byteAxis,
  countAxis,
  Granularity,
  Metric,
  OTHERS_COLOR,
  SERIES_COLORS,
} from "utils/usageChart";
import useAdmins from "hooks/useAdmins";
import useGetUser from "hooks/useGetUser";
import { useIsPhone } from "hooks/useIsPhone";

// ApexCharts is heavy; lazy-load it so it leaves the main bundle.
const ReactApexChart = lazyWithReload(() => import("react-apexcharts"));

type DateType = Date | null;

// "total" draws one series summed over every admin in view; "admins" stacks
// the top admins plus "Others", so the top edge is still the total.
type View = "total" | "admins";

// The page's views. Each fits about one phone screen; anything longer opens
// in a sheet. "admins" only exists for a sudo seeing more than one admin.
type TabKey = "overview" | "apps" | "admins" | "users";

// Spans this short (in days) are rendered hourly — a 1-day range as one
// daily bar is useless. Everything wider is aggregated per day.
const HOURLY_MAX_SPAN_DAYS = 2;

// Usernames reach the tooltip, which ApexCharts inserts as HTML.
const escapeHtml = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

// "+12", "−3", "0" — a real minus sign so the column lines up.
const signed = (n: number) =>
  `${n > 0 ? "+" : n < 0 ? "−" : ""}${numberWithCommas(Math.abs(n)) ?? ""}`;

// Change against the previous period, in percent; null when there is nothing to compare with.
const change = (cur: number, prev: number | undefined) =>
  prev === undefined || prev <= 0 ? null : ((cur - prev) / prev) * 100;

// The previous period must cover about as many buckets as the current one,
// or a history that starts mid-period reads as a "+3000%" jump.
const MIN_PREVIOUS_COVERAGE = 0.9;

export const Statistics: FC = () => {
  const { t, i18n } = useTranslation();
  const { colorMode } = useColorMode();
  const navigate = useNavigate();
  // Chart chrome per mode: primary / secondary / muted ink, hairline grid,
  // and the page surface (the 1px gap between stacked bands).
  const [
    inkLight, ink2Light, mutedLight, gridLight,
    inkDark, ink2Dark, mutedDark, gridDark, surfaceDark,
  ] = useToken("colors", [
    "gray.800", "gray.600", "gray.500", "gray.200",
    "gray.50", "gray.300", "gray.400", "gray.700", "gray.800",
  ]);
  const dark = colorMode === "dark";
  const ink = dark
    ? { primary: inkDark, secondary: ink2Dark, muted: mutedDark, grid: gridDark, surface: surfaceDark }
    : { primary: inkLight, secondary: ink2Light, muted: mutedLight, grid: gridLight, surface: "#ffffff" };

  const [startDate, setStartDate] = useState<DateType>(null);
  const [endDate, setEndDate] = useState<DateType>(null);
  const [metric, setMetric] = useState<Metric>("traffic");
  const [view, setView] = useState<View>("total");
  // 1M is the active default on mount (see useEffect below).
  const [preset, setPreset] = useState<Preset>("1m");
  // Admin filter (sudo only; "" = all admins). Non-sudo callers are locked to
  // their own data server-side regardless of this value.
  const { userData, getUserIsSuccess } = useGetUser();
  const isSudo = getUserIsSuccess && userData.is_sudo;
  const { admins } = useAdmins(isSudo);
  const [adminFilter, setAdminFilter] = useState<string>("");
  // Display granularity. Defaults follow the preset (1D -> hourly, wider ->
  // daily) but the user can flip it to view a day hour-by-hour.
  const [granularity, setGranularity] = useState<Granularity>("daily");

  // The open view lives in the URL (?tab=apps), so a refresh or a shared
  // link reopens it.
  const [params, setParams] = useSearchParams();
  const showAdminsTab = !!isSudo && !adminFilter && admins.length > 1;
  const tabs: TabKey[] = showAdminsTab ? ["overview", "apps", "admins", "users"] : ["overview", "apps", "users"];
  const asked = params.get("tab") as TabKey | null;
  const tab: TabKey = asked && tabs.includes(asked) ? asked : "overview";
  const setTab = (next: TabKey) => setParams(next === "overview" ? {} : { tab: next }, { replace: true });
  // When the tab row scrolls (a 320px phone), keep the open tab in view.
  const tabList = useRef<HTMLDivElement>(null);
  useEffect(() => {
    tabList.current
      ?.querySelector('[aria-selected="true"]')
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [tab]);

  // Each view fetches only what it shows; react-query keeps the rest cached.
  const range = { start: startDate, end: endDate, adminUsername: adminFilter };
  const usage = useAdminsUsage(range, tab === "overview" || tab === "admins");
  // Compared once the current range's own data is in, as of its fetch time.
  const previous = usePreviousUsage(
    range,
    usage.data && !usage.isPreviousData ? usage.dataUpdatedAt : 0,
    tab === "overview"
  );
  const clientApps = useClientApps(range, tab === "apps" || tab === "admins");
  const topUsers = useTopUsers(range, tab === "users");
  const fetching = usage.isFetching || previous.isFetching || clientApps.isFetching || topUsers.isFetching;
  const data = usage.data ?? null;
  const serverGranularity = usage.data?.granularity;
  useEffect(() => {
    // The server picks the granularity by span and only ships points at
    // that resolution; sync the display to it. A "daily" response has no
    // hourly points, so the hourly toggle is hidden (see render below).
    // Keyed on the VALUE so a background refetch of the same range does not
    // undo a manual hourly/daily switch.
    if (serverGranularity) setGranularity(serverGranularity);
  }, [serverGranularity]);
  // While a new range loads, the previous range's points stay on screen; a
  // daily response can't be drawn hourly, whatever the toggle says.
  const shownGranularity: Granularity = serverGranularity === "daily" ? "daily" : granularity;

  // Phone sizing: a shorter chart, and the tooltip pinned to the top-left so
  // the finger reading the chart does not cover it.
  const phone = useIsPhone();
  const chartHeight = useBreakpointValue({ base: 220, md: 340 }) ?? 220;
  // Most x-axis labels that fit side by side without colliding.
  const maxXLabels = useBreakpointValue({ base: 4, md: 8, lg: 10 }) ?? 6;

  // How many days back each preset spans (inclusive of today).
  const PRESET_DAYS: Record<Exclude<Preset, "custom">, number> = {
    "1d": 1,
    "1w": 7,
    "1m": 30,
  };

  const applyPreset = (p: Exclude<Preset, "custom">) => {
    const end = new Date();
    const start = dayjs(end)
      .subtract(PRESET_DAYS[p] - 1, "day")
      .toDate();
    setPreset(p);
    setStartDate(start);
    setEndDate(end);
    setGranularity(p === "1d" ? "hourly" : "daily");
  };

  useEffect(() => {
    // Default range on open: last 1 month (30 days), 1M active.
    applyPreset("1m");
  }, []);

  const onDateChange = (dates: [DateType, DateType]) => {
    const [start, end] = dates;
    setPreset("custom");
    // selectsRange gives [start, null] on the first click — even when a full
    // range is already selected, it starts a fresh range — and [start, end]
    // on the second. Mirror it directly.
    setStartDate(start);
    setEndDate(end);
    if (start && end) {
      // Same rule as the server: hourly while end - start <= 2 days (3 calendar days).
      const span = dayjs(end).diff(dayjs(start), "day");
      setGranularity(span <= HOURLY_MAX_SPAN_DAYS ? "hourly" : "daily");
    }
  };

  // All heavy work — bucket aggregation, totals, ranking, the "Others" fold,
  // range summaries and per-admin rows — recomputes ONLY when the data, metric
  // or granularity change, not on every render (e.g. ApexCharts hover re-renders).
  const {
    buckets,
    totals,
    series,
    rows,
    totalTraffic,
    avgUsers,
    avgDevices,
    peakTraffic,
    peakBucket,
  } = useMemo(
    () =>
      buildUsageChart(data, metric, shownGranularity, (count) =>
        t("statistics.others", { count })
      ),
    [data, metric, shownGranularity, t]
  );
  // The previous period's totals, for the tiles' change; none while either
  // side still shows an older range (keepPreviousData), or when it is only
  // partly recorded.
  const comparable = !usage.isPreviousData && !previous.isPreviousData;
  const before = useMemo(() => {
    if (!previous.data || !comparable) return null;
    const prev = buildUsageChart(previous.data, "traffic", previous.data.granularity as Granularity, () => "");
    const own = buildUsageChart(data, "traffic", previous.data.granularity as Granularity, () => "");
    return prev.buckets.length >= MIN_PREVIOUS_COVERAGE * own.buckets.length ? prev : null;
  }, [previous.data, data, comparable]);

  // Bucket-label format depends on granularity.
  const axisFormat = shownGranularity === "hourly" ? "MMM D HH:00" : "MMM D";
  const peakLabel = peakBucket ? dayjs(peakBucket).format(axisFormat) : "—";

  const isTraffic = metric === "traffic";
  const fmtValue = (val: number, decimals: number): string =>
    isTraffic
      ? (formatBytes(val, decimals) as string)
      : numberWithCommas(Math.round(val)) ?? "";

  // The by-admin view needs more than one admin in view (an unfiltered sudo)
  // and the width for its legend; on a phone the Admins tab ranks them instead.
  const multiAdmin = series.length > 1;
  const canStack = multiAdmin && !phone;
  const byAdmin = view === "admins" && canStack;
  const mode = dark ? "dark" : "light";
  const seriesColor = (i: number) =>
    series[i]?.isOthers ? OTHERS_COLOR[mode] : SERIES_COLORS[mode][i];
  const totalName = multiAdmin
    ? t("adminsUsage.total")
    : series[0]?.name ?? t("adminsUsage.total");
  const chartSeries = byAdmin
    ? series.map((s) => ({ name: s.name, data: s.values }))
    : [{ name: totalName, data: totals }];

  // The chart's readout: the peak bucket for traffic, the change over the
  // range for the gauges.
  const lastIdx = buckets.length - 1;
  const firstLabel = lastIdx >= 0 ? dayjs(buckets[0]).format(axisFormat) : "";
  const rangeDelta =
    lastIdx > 0 ? Math.round(totals[lastIdx]) - Math.round(totals[0]) : 0;
  // Today's traffic bar holds a partial day: drawn lighter and labelled "so
  // far", or it reads as a drop. (Gauges average whatever hours exist, and
  // hourly buckets are whole hours.)
  const partialIdx =
    isTraffic &&
    !byAdmin &&
    shownGranularity === "daily" &&
    lastIdx >= 0 &&
    buckets[lastIdx].slice(0, 10) === ashgabatToday().format("YYYY-MM-DD")
      ? lastIdx
      : -1;
  const partialColor = dark ? "#184f95" : "#86b6ef";

  // One readout for every series at the hovered bucket: the date, the total
  // (with its change since the range start for gauges), then each admin by
  // value with "Others" last. Values lead, names follow.
  const renderTooltip = (i: number): string => {
    const b = buckets[i];
    if (!b) return "";
    const row = (color: string, value: string, name: string) =>
      `<div style="display:flex;align-items:center;gap:8px;margin-top:3px">` +
      `<span style="flex:none;width:12px;height:3px;border-radius:2px;background:${color}"></span>` +
      `<span style="min-width:44px;text-align:right;font-weight:600;color:${ink.primary};font-variant-numeric:tabular-nums">${escapeHtml(value)}</span>` +
      `<span style="color:${ink.secondary};white-space:nowrap">${escapeHtml(name)}</span></div>`;
    let html =
      `<div style="padding:8px 12px;font-size:12px;line-height:1.4;min-width:170px">` +
      `<div style="color:${ink.muted}">${escapeHtml(dayjs(b).format(axisFormat))}` +
      (i === partialIdx ? ` (${escapeHtml(t("statistics.soFar"))})` : "") +
      `</div>` +
      `<div style="display:flex;align-items:baseline;gap:6px;margin-top:2px">` +
      `<span style="font-size:16px;font-weight:600;color:${ink.primary}">${escapeHtml(fmtValue(totals[i], 2))}</span>` +
      `<span style="color:${ink.secondary}">${escapeHtml(totalName)}</span></div>`;
    if (!isTraffic && i > 0) {
      const d = Math.round(totals[i]) - Math.round(totals[0]);
      html += `<div style="color:${ink.secondary}">${escapeHtml(
        t("statistics.sinceStart", { delta: signed(d), date: firstLabel })
      )}</div>`;
    }
    if (byAdmin) {
      const order = series
        .map((s, idx) => ({ s, idx }))
        .sort(
          (a, z) =>
            Number(a.s.isOthers) - Number(z.s.isOthers) || z.s.values[i] - a.s.values[i]
        );
      html += `<div style="margin-top:6px;padding-top:4px;border-top:1px solid ${ink.grid}">`;
      for (const { s, idx } of order) html += row(seriesColor(idx), fmtValue(s.values[i], 2), s.name);
      html += `</div>`;
    }
    return html + `</div>`;
  };

  // Label every Nth bucket so the axis never crowds, whatever the span; an
  // hourly axis within one day needs only the hour, and the longer
  // "MMM D HH:00" labels of a multi-day hourly range get fewer slots.
  const tickFormat =
    shownGranularity === "hourly" &&
    lastIdx >= 0 &&
    buckets[0].slice(0, 10) === buckets[lastIdx].slice(0, 10)
      ? "HH:00"
      : axisFormat;
  const xLabels =
    tickFormat === "MMM D HH:00" ? Math.max(3, Math.floor(maxXLabels * 0.6)) : maxXLabels;
  const labelStep = Math.max(1, Math.ceil(buckets.length / xLabels));
  const chartType: "bar" | "area" = isTraffic ? "bar" : "area";
  // Both views top out at the per-bucket total (the stack's top edge).
  const maxTotal = Math.max(0, ...totals);
  const yAxis = isTraffic ? byteAxis(maxTotal) : countAxis(maxTotal);

  // react-apexcharts compares options as JSON, so a data-only change (a
  // cached admin filter, a background refetch) reaches ApexCharts as
  // updateSeries alone and it keeps the FIRST function options it saw. Every
  // callback therefore reads the current render through this ref.
  const latest = useRef({ renderTooltip, buckets, labelStep, tickFormat, yAxis, partialIdx });
  latest.current = { renderTooltip, buckets, labelStep, tickFormat, yAxis, partialIdx };

  const chartOptions: ApexOptions = {
    chart: {
      type: chartType,
      stacked: byAdmin,
      toolbar: { show: false },
      zoom: { enabled: false },
      animations: { enabled: false },
      fontFamily: "inherit",
      background: "transparent",
    },
    plotOptions: {
      // Rounded tops only on single bars: ApexCharts 3.40 rounds just the
      // last stacked series, so a stack whose top admin is 0 loses it.
      bar: {
        columnWidth: "70%",
        borderRadius: byAdmin ? 0 : 3,
        borderRadiusApplication: "end",
      },
    },
    // Never pass undefined for stroke/fill: that overrides ApexCharts' default
    // objects and crashes it ("Cannot read properties of undefined (reading
    // 'colors')"). `colors` is always explicit too, since ApexCharts merges
    // new options into the old ones and would keep a previous view's value.
    // Stacked bands are split by a 1px surface-coloured stroke; the single
    // total area gets a 2px line over a faint gradient. Straight segments: a
    // smoothed curve invents values between the buckets.
    stroke: byAdmin
      ? { curve: "straight", width: 1, colors: [ink.surface] }
      : isTraffic
      ? { width: 0, colors: [ink.surface] }
      : { curve: "straight", width: 2, colors: [seriesColor(0)] },
    fill: isTraffic
      ? { type: "solid", opacity: 1 }
      : byAdmin
      ? { type: "solid", opacity: 0.9 }
      : {
          type: "gradient",
          gradient: { shadeIntensity: 0, opacityFrom: 0.3, opacityTo: 0.02, stops: [0, 100] },
        },
    dataLabels: { enabled: false },
    markers: {
      size: 0,
      strokeColors: ink.surface,
      strokeWidth: 2,
      hover: { size: byAdmin ? 0 : 5 },
    },
    colors: byAdmin
      ? series.map((_, i) => seriesColor(i))
      : isTraffic
      ? [
          ({ dataPointIndex }: { dataPointIndex: number }) =>
            dataPointIndex === latest.current.partialIdx ? partialColor : seriesColor(0),
        ]
      : [seriesColor(0)],
    states: {
      hover: { filter: { type: "lighten", value: 0.04 } },
      active: { filter: { type: "none" } },
    },
    xaxis: {
      categories: buckets,
      axisBorder: { color: ink.grid },
      axisTicks: { show: false },
      tooltip: { enabled: false },
      crosshairs: {
        stroke: { color: ink.muted, width: 1, dashArray: 0 },
        fill: { type: "solid", color: ink.grid },
      },
      labels: {
        rotate: 0,
        hideOverlappingLabels: false,
        style: { colors: ink.muted, fontSize: "12px" },
        formatter: (val: string) => {
          const l = latest.current;
          return l.buckets.indexOf(val) % l.labelStep === 0 ? dayjs(val).format(l.tickFormat) : "";
        },
      },
    },
    yaxis: {
      min: 0,
      max: yAxis.max,
      tickAmount: yAxis.tickAmount,
      labels: {
        style: { colors: ink.muted, fontSize: "12px" },
        formatter: (val: number) => latest.current.yAxis.format(val),
      },
    },
    legend: {
      show: byAdmin,
      position: "bottom",
      horizontalAlign: "left",
      fontSize: "12px",
      labels: { colors: ink.secondary },
      // ApexCharts sets legend text as innerHTML; usernames are free text.
      formatter: (name: string) => escapeHtml(name),
      markers: { width: 10, height: 10, radius: 2 },
      itemMargin: { horizontal: 8, vertical: 4 },
      // The tooltip total always covers every admin, so hiding a series
      // from the legend would make the chart and the readout disagree.
      onItemClick: { toggleDataSeries: false },
      onItemHover: { highlightDataSeries: true },
    },
    tooltip: {
      theme: colorMode,
      shared: true,
      intersect: false,
      fixed: { enabled: phone, position: "topLeft", offsetX: 0, offsetY: 0 },
      custom: ({ dataPointIndex }: { dataPointIndex: number }) =>
        latest.current.renderTooltip(dataPointIndex),
      // ApexCharts 3.40 still fills its default tooltip rows after `custom`
      // replaced them, as innerHTML of now-detached nodes — which a name like
      // `<img onerror=…>` still executes in. Escape the names it writes there.
      y: { title: { formatter: (name: string) => escapeHtml(name) } },
    },
    grid: {
      borderColor: ink.grid,
      strokeDashArray: 0,
      xaxis: { lines: { show: false } },
      padding: { left: 8, right: 8 },
    },
  };

  // Admins with no snapshots in the range still come back (empty points), so
  // "no data" is no buckets, not no admins.
  const hasData = !!data && buckets.length > 0;
  const vsPrevious = t("statistics.vsPrevious");

  const overview = usage.isError && !data ? (
    <LoadError message={t("adminsUsage.loadError")} onRetry={() => usage.refetch()} />
  ) : (
    <VStack align="stretch" spacing={4}>
      {/* The headline numbers are also the chart's metric switch. */}
      <SimpleGrid columns={3} spacing={2} role="group" aria-label={t("statistics.metric")}>
        <StatTile
          label={t("adminsUsage.metric.traffic")}
          // One decimal: "560.8 GB" fits a third of a 320px phone, "560.84 GB" does not.
          value={formatBytes(totalTraffic, 1)}
          caption={t("statistics.inRange")}
          delta={<Delta pct={change(totalTraffic, before?.totalTraffic)} title={vsPrevious} />}
          onClick={() => setMetric("traffic")}
          isSelected={metric === "traffic"}
          isLoading={!data}
        />
        <StatTile
          label={t("adminsUsage.metric.devices")}
          value={numberWithCommas(avgDevices)}
          caption={t("statistics.dailyAvg")}
          delta={<Delta pct={change(avgDevices, before?.avgDevices)} title={vsPrevious} />}
          onClick={() => setMetric("devices")}
          isSelected={metric === "devices"}
          isLoading={!data}
        />
        <StatTile
          label={t("adminsUsage.metric.users")}
          value={numberWithCommas(avgUsers)}
          caption={t("statistics.dailyAvg")}
          delta={<Delta pct={change(avgUsers, before?.avgUsers)} title={vsPrevious} />}
          onClick={() => setMetric("users")}
          isSelected={metric === "users"}
          isLoading={!data}
        />
      </SimpleGrid>

      {!data ? (
        <Skeleton h={`${chartHeight + 56}px`} borderRadius="lg" />
      ) : !hasData ? (
        <EmptyState compact title={t("adminsUsage.noData")} />
      ) : (
        <Box>
          <Flex justify="space-between" align="flex-end" gap={3} flexWrap="wrap" mb={1}>
            <Box minW={0}>
              <Text fontSize="xs" color="gray.600" _dark={{ color: "gray.400" }}>
                {isTraffic
                  ? t("statistics.peakOn", { date: peakLabel })
                  : t("statistics.changeSince", { date: firstLabel })}
              </Text>
              <Text fontSize="xl" fontWeight="semibold" lineHeight="short" sx={{ fontVariantNumeric: "tabular-nums" }}>
                {isTraffic ? formatBytes(peakTraffic) : signed(rangeDelta)}
              </Text>
            </Box>
            <HStack spacing={2}>
              {canStack && (
                <SegmentedControl<View>
                  value={view}
                  onChange={setView}
                  size="xs"
                  options={[
                    { value: "total", label: t("statistics.viewTotal") },
                    { value: "admins", label: t("statistics.viewByAdmin") },
                  ]}
                />
              )}
              {/* Granularity toggle only when the server sent hourly points. */}
              {serverGranularity === "hourly" && (
                <SegmentedControl<Granularity>
                  value={granularity}
                  onChange={setGranularity}
                  size="xs"
                  options={[
                    { value: "hourly", label: t("statistics.hourly") },
                    { value: "daily", label: t("statistics.daily") },
                  ]}
                />
              )}
            </HStack>
          </Flex>
          <Suspense fallback={<Skeleton h={`${chartHeight}px`} borderRadius="md" />}>
            <Box sx={{ touchAction: "pan-y" }}>
              <ReactApexChart
                key={`${metric}-${shownGranularity}-${byAdmin}-${colorMode}-${i18n.language}-${buckets.length}-${labelStep}-${phone}-${partialIdx}`}
                options={chartOptions}
                series={chartSeries}
                type={chartType}
                height={chartHeight}
                width="100%"
              />
            </Box>
          </Suspense>
        </Box>
      )}
    </VStack>
  );

  const tabLabel: Record<TabKey, string> = {
    overview: t("statistics.tabOverview"),
    apps: t("statistics.tabApps"),
    admins: t("statistics.tabAdmins"),
    users: t("statistics.tabUsers"),
  };

  return (
    <Page>
      <PageHeader title={t("statistics.title")} backLabel={t("statistics.back")} onBack={() => navigate("/")} />

      <Tabs
        index={tabs.indexOf(tab)}
        onChange={(i) => setTab(tabs[i])}
        isLazy
        colorScheme="primary"
      >
        {/* Filters and views stay in reach while the view scrolls. */}
        <StickyBar busy={fetching}>
          <FilterBar
            preset={preset}
            start={startDate}
            end={endDate}
            onPreset={applyPreset}
            onCustom={onDateChange}
            admins={isSudo ? admins : undefined}
            admin={adminFilter}
            onAdmin={(username) => {
              setAdminFilter(username);
              // One admin has no Admins tab; don't leave ?tab=admins to jump
              // back to when the filter is cleared.
              if (username && tab === "admins") setTab("overview");
            }}
          />
          {/* Tabs fill the width when they fit and scroll sideways when they
              don't (Russian or Turkmen labels on a 320px phone). */}
          <TabList
            ref={tabList}
            mt={2}
            borderBottomWidth="1px"
            overflowX="auto"
            overflowY="hidden"
            sx={{ scrollbarWidth: "none", "&::-webkit-scrollbar": { display: "none" } }}
          >
            {tabs.map((k) => (
              <Tab
                key={k}
                flex={phone ? "1 0 auto" : undefined}
                fontSize="sm"
                fontWeight="medium"
                px={{ base: 1, md: 4 }}
                minH="44px"
                whiteSpace="nowrap"
              >
                {tabLabel[k]}
              </Tab>
            ))}
          </TabList>
        </StickyBar>
        <TabPanels>
          {tabs.map((k) => (
            <TabPanel key={k} px={0} pt={4}>
              {k === "overview" ? (
                overview
              ) : k === "apps" ? (
                <ClientApps
                  data={clientApps.data}
                  isError={clientApps.isError}
                  onRetry={() => clientApps.refetch()}
                  showAdmins={showAdminsTab}
                />
              ) : k === "admins" ? (
                <AdminsRanking
                  rows={data ? rows : undefined}
                  apps={clientApps.data}
                  usageError={usage.isError && !data}
                  appsError={clientApps.isError && !clientApps.data}
                  onRetry={() => {
                    if (usage.isError) usage.refetch();
                    if (clientApps.isError) clientApps.refetch();
                  }}
                  onSelectAdmin={(username) => {
                    setAdminFilter(username);
                    setTab("overview");
                  }}
                />
              ) : (
                <TopUsers
                  data={topUsers.data}
                  isError={topUsers.isError}
                  onRetry={() => topUsers.refetch()}
                  showOwner={!!isSudo && !adminFilter}
                />
              )}
            </TabPanel>
          ))}
        </TabPanels>
      </Tabs>
    </Page>
  );
};

export default Statistics;
