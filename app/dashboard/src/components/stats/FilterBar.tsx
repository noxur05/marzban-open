import {
  Box,
  Flex,
  Icon,
  Input,
  InputGroup,
  InputLeftElement,
  Text,
  useBreakpointValue,
  useDisclosure,
  VStack,
} from "@chakra-ui/react";
import {
  CalendarIcon,
  MagnifyingGlassIcon,
  UserIcon,
} from "@heroicons/react/24/outline";
import dayjs from "dayjs";
import { FC, useState } from "react";
import ReactDatePicker from "react-datepicker";
import { useTranslation } from "react-i18next";
import { FilterChip, OptionRow } from "components/shared/FilterChip";

// Presets drive both the date range and the bucket granularity.
export type Preset = "1d" | "1w" | "1m" | "custom";
type DateType = Date | null;

const muted = { color: "gray.600", _dark: { color: "gray.400" } };

type FilterBarProps = {
  preset: Preset;
  start: DateType;
  end: DateType;
  onPreset: (p: Exclude<Preset, "custom">) => void;
  onCustom: (dates: [DateType, DateType]) => void;
  /** Sudo only: the admin filter ("" = every admin). */
  admins?: { username: string; is_sudo: boolean }[];
  admin: string;
  onAdmin: (username: string) => void;
};

/**
 * The Statistics page's filters as two chips — date range and (sudo) admin —
 * instead of rows of controls above the data. The range chip names the
 * range ("Last 30 days"), the admin chip the admin, with a one-tap clear.
 */
export const FilterBar: FC<FilterBarProps> = ({
  preset,
  start,
  end,
  onPreset,
  onCustom,
  admins,
  admin,
  onAdmin,
}) => {
  const { t, i18n } = useTranslation();
  const range = useDisclosure();
  const people = useDisclosure();
  const [customOpen, setCustomOpen] = useState(preset === "custom");
  const [query, setQuery] = useState("");
  const monthsShown = useBreakpointValue({ base: 1, md: 2 });

  const presetLabel: Record<Exclude<Preset, "custom">, string> = {
    "1d": t("statistics.rangeToday"),
    "1w": t("statistics.rangeLast7"),
    "1m": t("statistics.rangeLast30"),
  };
  // The chip says it short ("30 days"); the sheet's options say it in full.
  const chipLabel: Record<Exclude<Preset, "custom">, string> = {
    "1d": t("statistics.rangeToday"),
    "1w": t("statistics.days", { count: 7 }),
    "1m": t("statistics.days", { count: 30 }),
  };
  const rangeLabel =
    preset === "custom"
      ? `${start ? dayjs(start).format("MMM D") : "…"} – ${
          end ? dayjs(end).format("MMM D") : "…"
        }`
      : chipLabel[preset];

  const pickPreset = (p: Exclude<Preset, "custom">) => {
    onPreset(p);
    setCustomOpen(false);
    range.onClose();
  };
  const pickDates = (dates: [DateType, DateType]) => {
    onCustom(dates);
    if (dates[0] && dates[1]) range.onClose();
  };
  const pickAdmin = (username: string) => {
    onAdmin(username);
    closePeople();
  };
  const closePeople = () => {
    setQuery("");
    people.onClose();
  };
  // Dismissed after picking only a start date: that day alone, rather than a
  // range with no end (every query off, the old range left on screen).
  const closeRange = () => {
    if (preset === "custom" && start && !end) onCustom([start, start]);
    range.onClose();
  };

  const q = query.trim().toLowerCase();
  const shown = (admins ?? []).filter(
    (a) => !q || a.username.toLowerCase().includes(q)
  );

  return (
    <Flex gap={2} flexWrap="wrap">
      <FilterChip
        icon={CalendarIcon}
        label={rangeLabel}
        title={t("statistics.dateRange")}
        isOpen={range.isOpen}
        onOpen={range.onOpen}
        onClose={closeRange}
      >
        <VStack align="stretch" spacing={0.5}>
          {(["1d", "1w", "1m"] as const).map((p) => (
            <OptionRow
              key={p}
              selected={preset === p}
              onClick={() => pickPreset(p)}
            >
              {presetLabel[p]}
            </OptionRow>
          ))}
          <OptionRow
            selected={preset === "custom"}
            onClick={() => setCustomOpen((v) => !v)}
          >
            {t("statistics.rangeCustom")}
          </OptionRow>
          {customOpen && (
            <Box alignSelf="center" pt={1} overflowX="auto" maxW="full">
              <ReactDatePicker
                locale={i18n.language.toLocaleLowerCase()}
                selected={start}
                onChange={pickDates}
                startDate={start}
                endDate={end}
                selectsRange
                maxDate={new Date()}
                monthsShown={monthsShown}
                peekNextMonth={false}
                inline
              />
            </Box>
          )}
          <Text fontSize="xs" px={3} pt={2} {...muted}>
            {t("statistics.ashgabatTime")}
          </Text>
        </VStack>
      </FilterChip>

      {admins && (
        <FilterChip
          icon={UserIcon}
          label={admin || t("filters.allAdmins")}
          title={t("adminsUsage.admin")}
          isOpen={people.isOpen}
          onOpen={people.onOpen}
          onClose={closePeople}
          onClear={admin ? () => onAdmin("") : undefined}
          truncate={!!admin}
          clearLabel={t("statistics.clearAdmin")}
        >
          <VStack align="stretch" spacing={2}>
            {admins.length > 8 && (
              <InputGroup size="md">
                <InputLeftElement pointerEvents="none">
                  <Icon
                    as={MagnifyingGlassIcon}
                    boxSize="16px"
                    color="gray.400"
                  />
                </InputLeftElement>
                <Input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder={t("statistics.searchAdmin")}
                  aria-label={t("statistics.searchAdmin")}
                />
              </InputGroup>
            )}
            <VStack
              align="stretch"
              spacing={0.5}
              maxH={{ md: "360px" }}
              overflowY={{ md: "auto" }}
            >
              {!q && (
                <OptionRow selected={!admin} onClick={() => pickAdmin("")}>
                  {t("filters.allAdmins")}
                </OptionRow>
              )}
              {shown.map((a) => (
                <OptionRow
                  key={a.username}
                  selected={admin === a.username}
                  onClick={() => pickAdmin(a.username)}
                >
                  {a.username}
                  {a.is_sudo ? " (sudo)" : ""}
                </OptionRow>
              ))}
            </VStack>
          </VStack>
        </FilterChip>
      )}
    </Flex>
  );
};
