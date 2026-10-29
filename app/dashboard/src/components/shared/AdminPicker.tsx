import {
  Badge,
  Box,
  Button,
  Checkbox,
  HStack,
  Input,
  InputGroup,
  InputLeftElement,
  SimpleGrid,
  Text,
  Tooltip,
  VStack,
} from "@chakra-ui/react";
import { MagnifyingGlassIcon } from "@heroicons/react/24/outline";
import useAdmins from "hooks/useAdmins";
import { FC, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { FormField } from "./FormField";
import { Hint } from "./Hint";

export type AdminPickerProps = {
  value: number[];
  onChange: (ids: number[]) => void;
  helper?: string;
  /** What an EMPTY selection means for this registry. External subscriptions
   * and manual hosts fail closed (nobody receives the host); the hosts dialog
   * is the opposite. The picker says it out loud either way. */
  emptyMeans: "nobody" | "everybody";
};

/** Above this many admins the picker shows a filter box. */
const FILTER_THRESHOLD = 8;

/**
 * Inline multi-select of admins: a checkbox grid you can scan and tick in
 * place (no popover to open), a filter once the list is long, select-all /
 * clear, and a running "n of m" count. Wrapped in a labelled field.
 */
export const AdminPicker: FC<AdminPickerProps> = ({ value, onChange, helper, emptyMeans }) => {
  const { t } = useTranslation();
  const { admins, isLoadingAdmins } = useAdmins(true);
  const [filter, setFilter] = useState("");
  // The SUDOERS env-bootstrap admin has no row and no id, so it is not assignable.
  const candidates = useMemo(
    () =>
      admins
        .filter((a): a is typeof a & { id: number } => typeof a.id === "number")
        .sort((a, b) => a.username.localeCompare(b.username)),
    [admins]
  );
  const selected = new Set(value);
  const q = filter.trim().toLowerCase();
  const visible = q ? candidates.filter((a) => a.username.toLowerCase().includes(q)) : candidates;
  const toggle = (id: number) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(Array.from(next));
  };
  const none = selected.size === 0;

  return (
    <FormField
      label={t("adminPicker.label")}
      labelAddon={
        <HStack spacing={3}>
          <Hint>{t("adminPicker.selectedOf", { count: selected.size, total: candidates.length })}</Hint>
          {/* With a filter active, "Select all" adds the VISIBLE admins to the
              selection; without one it selects everybody. */}
          <Button
            size="xs"
            variant="link"
            onClick={() =>
              onChange(Array.from(new Set([...(q ? value : []), ...visible.map((a) => a.id)])))
            }
            isDisabled={visible.length === 0}
          >
            {q ? t("adminPicker.selectVisible", { count: visible.length }) : t("adminPicker.selectAll")}
          </Button>
          <Button size="xs" variant="link" onClick={() => onChange([])} isDisabled={none}>
            {t("adminPicker.clear")}
          </Button>
        </HStack>
      }
      helper={helper}
    >
      <VStack align="stretch" spacing={2}>
        {candidates.length > FILTER_THRESHOLD && (
          <InputGroup size="sm">
            <InputLeftElement pointerEvents="none">
              <MagnifyingGlassIcon width={14} height={14} />
            </InputLeftElement>
            <Input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder={t("adminPicker.filter")}
            />
          </InputGroup>
        )}
        <Box
          borderWidth="1px"
          borderRadius="md"
          borderColor={none && emptyMeans === "nobody" ? "orange.300" : "light-border"}
          _dark={{ borderColor: none && emptyMeans === "nobody" ? "orange.500" : "gray.600" }}
          p={3}
          maxH="220px"
          overflowY="auto"
        >
          {isLoadingAdmins ? (
            <Hint>…</Hint>
          ) : candidates.length === 0 ? (
            <Hint>{t("adminPicker.noAdmins")}</Hint>
          ) : visible.length === 0 ? (
            <Hint>{t("adminPicker.noMatch")}</Hint>
          ) : (
            <SimpleGrid columns={{ base: 1, sm: 2, md: 3 }} spacingX={4} spacingY={2}>
              {visible.map((a) => (
                <Checkbox
                  key={a.id}
                  size="sm"
                  colorScheme="primary"
                  isChecked={selected.has(a.id)}
                  onChange={() => toggle(a.id)}
                >
                  <HStack spacing={1}>
                    <Text fontSize="sm" noOfLines={1}>{a.username}</Text>
                    {a.is_sudo && (
                      <Badge colorScheme="purple" fontSize="0.6rem">sudo</Badge>
                    )}
                  </HStack>
                </Checkbox>
              ))}
            </SimpleGrid>
          )}
        </Box>
        {none && (
          <Text fontSize="xs" color={emptyMeans === "nobody" ? "orange.500" : undefined}>
            {emptyMeans === "nobody" ? t("adminPicker.warnNobody") : t("adminPicker.everybody")}
          </Text>
        )}
      </VStack>
    </FormField>
  );
};

/**
 * Compact read-only summary for a list row: the first few names as chips,
 * then "+N" carrying the rest in a tooltip — a row with 12 admins stays one
 * line tall.
 */
export const AdminChips: FC<{ ids: number[]; emptyMeans: "nobody" | "everybody"; max?: number }> = ({
  ids,
  emptyMeans,
  max = 2,
}) => {
  const { t } = useTranslation();
  const { admins } = useAdmins(true);
  const byId = new Map(admins.map((a) => [a.id, a.username]));
  if (ids.length === 0) {
    return (
      <Badge colorScheme={emptyMeans === "nobody" ? "orange" : "gray"} fontSize="xs">
        {emptyMeans === "nobody" ? t("adminPicker.nobody") : t("adminPicker.everybody")}
      </Badge>
    );
  }
  const names = ids.map((id) => byId.get(id) ?? `#${id}`).sort((a, b) => a.localeCompare(b));
  const shown = names.slice(0, max);
  const rest = names.slice(max);
  return (
    <Tooltip label={names.join(", ")} hasArrow isDisabled={rest.length === 0} placement="top-start">
      <HStack spacing={1} flexWrap="nowrap" cursor={rest.length ? "default" : undefined}>
        {shown.map((n) => (
          <Badge key={n} variant="subtle" colorScheme="primary" fontSize="xs" maxW="110px" noOfLines={1}>
            {n}
          </Badge>
        ))}
        {rest.length > 0 && (
          <Badge variant="outline" fontSize="xs" whiteSpace="nowrap">
            +{rest.length}
          </Badge>
        )}
      </HStack>
    </Tooltip>
  );
};
