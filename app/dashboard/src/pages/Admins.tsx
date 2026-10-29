import {
  Box,
  Button,
  Flex,
  HStack,
  Icon,
  Input,
  InputGroup,
  InputLeftElement,
  Skeleton,
  Text,
  useDisclosure,
  VStack,
} from "@chakra-ui/react";
import {
  ArrowPathIcon,
  ArrowsUpDownIcon,
  FunnelIcon,
  MagnifyingGlassIcon,
  PlusIcon,
} from "@heroicons/react/24/outline";
import { AdminEditDialog } from "components/admins/AdminEditDialog";
import { AdminSheet } from "components/admins/AdminSheet";
import { CreateAdminSheet } from "components/admins/CreateAdminSheet";
import { usernameProblem } from "components/admins/rules";
import { commonDomain, TraitBadges, TraitKey, useTraits } from "components/admins/traits";
import {
  ConfirmModal,
  EmptyState,
  FilterChip,
  LoadError,
  OptionRow,
  Page,
  PageHeader,
  RankedItem,
  RankedList,
  StickyBar,
} from "components/shared";
import useAdmins, { AdminListItem } from "hooks/useAdmins";
import { useAppToast } from "hooks/useAppToast";
import { useRegenerateSubs } from "hooks/useRegenerateSubs";
import { FC, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLoaderData, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { formatBytes, numberWithCommas } from "utils/formatByte";

type Access = "all" | Extract<TraitKey, "sudo" | "unrestricted" | "announces" | "outline">;
type Sort = "name" | "users" | "traffic";
const ACCESS: Access[] = ["all", "sudo", "unrestricted", "announces", "outline"];
const SORTS: Sort[] = ["name", "users", "traffic"];

const muted = { color: "gray.600", _dark: { color: "gray.400" } };
const hasRow = (a: AdminListItem) => typeof a.id === "number";

/**
 * Sudo-only admin management, in the Statistics page's language: one list
 * with a search and two chips, a sheet per admin. Replaces the old Admins
 * modal (a desktop table that opened a dialog that opened a confirm).
 */
const Admins: FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useAppToast();
  const { admins, isLoadingAdmins, isErrorAdmins, isFetchingAdmins, refetchAdmins } = useAdmins(true, true);
  const traitsOf = useTraits();
  // The route's loader: the signed-in sudo, who is not in the list when an
  // env sudoer.
  const signedIn = (useLoaderData() as AdminListItem | null)?.username ?? "";
  // Loaded, even if empty: an env sudoer alone makes the first admin here.
  const listReady = !isLoadingAdmins && !(isErrorAdmins && !admins.length);
  const domain = useMemo(() => commonDomain(admins), [admins]);

  const [query, setQuery] = useState("");
  const [access, setAccess] = useState<Access>("all");
  const [sort, setSort] = useState<Sort>("name");
  const accessChip = useDisclosure();
  const sortChip = useDisclosure();
  const [editing, setEditing] = useState<string | null>(null);
  const [prefill, setPrefill] = useState("");
  // Tinted in the list until the page is left.
  const [createdName, setCreatedName] = useState<string | null>(null);
  const [confirmAll, setConfirmAll] = useState(false);
  const regenAll = useRegenerateSubs();

  // The open sheet lives in the URL (?admin=name, ?new), so a phone's Back
  // button closes it instead of leaving the page.
  const [params, setParams] = useSearchParams();
  const openName = params.get("admin");
  const creating = params.has("new");
  const open = admins.find((a) => a.username === openName && hasRow(a)) ?? null;
  const closing = useRef(false);
  useEffect(() => {
    closing.current = false;
    // Back (or any close) takes the settings dialog with the sheet: left
    // open on its own, the next Back would leave the page under it.
    if (!openName) setEditing(null);
  }, [openName, creating]);
  // A link (or an admin deleted elsewhere) naming nobody: close it like any
  // sheet, so the next one opened doesn't stack on a dead history entry.
  useEffect(() => {
    if (openName && !open && admins.length && !isFetchingAdmins) closeSheet();
  }, [openName, open, admins.length, isFetchingAdmins]);
  const openAdmin = (username: string) => setParams({ admin: username }, { state: { sheet: true } });
  const openNew = (name = "") => {
    setPrefill(name);
    setParams({ new: "1" }, { state: { sheet: true } });
  };
  // Closing the login card lands on the new row.
  const scrollTo = useRef<string | null>(null);
  // Read by a create that finishes after its sheet was closed: the submit
  // holds an older render's callbacks.
  const now = useRef({ mounted: false, creating, openName });
  now.current.creating = creating;
  now.current.openName = openName;
  useEffect(() => {
    // Set here too: StrictMode runs the cleanup once on mount.
    now.current.mounted = true;
    return () => {
      now.current.mounted = false;
    };
  }, []);
  /** Whether the login card will show. */
  const onCreated = (username: string): boolean => {
    setCreatedName(username);
    scrollTo.current = username;
    // Nothing on the page may hide the new row.
    setAccess("all");
    setQuery((cur) => (username.toLowerCase().includes(cur.trim().toLowerCase()) ? cur : ""));
    const { mounted, creating, openName } = now.current;
    if (creating) return true;
    // Back while the request ran: reopened on its login card, the one place
    // the password is ever shown. Not over another sheet, nor once the page
    // is left: said in a toast instead.
    if (mounted && !openName) {
      setParams({ new: "1" }, { state: { sheet: true } });
      return true;
    }
    toast.success(t("admins.create.doneTitle"), username);
    return false;
  };
  // From the new admin's login to its sheet, in the same history entry.
  const showCreated = (username: string) => {
    scrollTo.current = null;
    setParams({ admin: username }, { replace: true, state: location.state });
  };
  // Not on the URL change: Back's scroll restore lands after it.
  const createClosed = () => {
    const name = scrollTo.current;
    scrollTo.current = null;
    if (name) document.querySelector(`[data-key="${CSS.escape(name)}"]`)?.scrollIntoView({ block: "center" });
  };
  const closeSheet = () => {
    // Once per open sheet: a second step back would leave the page.
    if ((!openName && !creating) || closing.current) return;
    closing.current = true;
    // Opened from the list: step back, so Back doesn't reopen it. Opened
    // from a link: just drop the parameter.
    if ((location.state as { sheet?: boolean } | null)?.sheet) navigate(-1);
    else setParams({}, { replace: true });
  };

  const accessLabel: Record<Access, string> = {
    all: t("filters.allAdmins"),
    sudo: t("admins.trait.sudo"),
    unrestricted: t("admins.trait.unrestricted"),
    announces: t("admins.trait.announces"),
    outline: t("admins.trait.outline"),
  };
  const sortLabel: Record<Sort, string> = {
    name: t("admins.sort.name"),
    users: t("admins.sort.users"),
    traffic: t("admins.sort.traffic"),
  };

  const q = query.trim().toLowerCase();
  const byName = (x: AdminListItem, y: AdminListItem) =>
    x.username.localeCompare(y.username, undefined, { sensitivity: "base" });
  const metric = (a: AdminListItem) => (sort === "traffic" ? a.users_usage ?? 0 : a.users_count ?? 0);
  const rows = admins
    .map((a) => ({ a, traits: traitsOf(a, domain) }))
    .filter(({ a }) => !q || a.username.toLowerCase().includes(q))
    .filter(
      ({ traits }) =>
        access === "all" || traits.some((tr) => tr.key === access || (access === "sudo" && tr.key === "env"))
    )
    .sort((x, y) => (sort === "name" ? 0 : metric(y.a) - metric(x.a)) || byName(x.a, y.a));

  const items: RankedItem[] = rows.map(({ a, traits }) => ({
    key: a.username,
    title: a.username,
    badges: traits.length ? <TraitBadges traits={traits} /> : undefined,
    value: !hasRow(a)
      ? "—"
      : sort === "traffic"
        ? formatBytes(a.users_usage ?? 0)
        : numberWithCommas(a.users_count ?? 0),
    onClick: hasRow(a) ? () => openAdmin(a.username) : undefined,
    highlight: a.username === createdName,
  }));
  const typed = query.trim();

  const runAll = async () => {
    try {
      const res = await regenAll.mutateAsync({});
      toast.success(t("regen.started", { count: res?.queued ?? 0 }));
      setConfirmAll(false);
    } catch (e) {
      toast.error(e, t("regen.error"));
    }
  };

  return (
    <Page>
      <PageHeader title={t("admins.title")} backLabel={t("statistics.back")} onBack={() => navigate("/")} />

      <StickyBar busy={isFetchingAdmins && !isLoadingAdmins}>
        <HStack spacing={2}>
          <InputGroup flex="1" maxW={{ md: "420px" }}>
            <InputLeftElement pointerEvents="none" h="44px">
              <Icon as={MagnifyingGlassIcon} boxSize="16px" color="gray.400" />
            </InputLeftElement>
            <Input
              type="search"
              h="44px"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("statistics.searchAdmin")}
              aria-label={t("statistics.searchAdmin")}
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
          </InputGroup>
          <Button
            h="44px"
            flexShrink={0}
            colorScheme="primary"
            leftIcon={<Icon as={PlusIcon} boxSize="18px" />}
            // The name check and the default domain come from the list.
            isDisabled={!listReady}
            onClick={() => openNew()}
          >
            {t("admins.new")}
          </Button>
        </HStack>
        <Flex gap={2} wrap="wrap" pt={2} pb={2}>
          <FilterChip
            icon={FunnelIcon}
            label={access === "all" ? t("admins.filter.access") : accessLabel[access]}
            title={t("admins.filter.access")}
            isOpen={accessChip.isOpen}
            onOpen={accessChip.onOpen}
            onClose={accessChip.onClose}
            onClear={access === "all" ? undefined : () => setAccess("all")}
            clearLabel={t("admins.filter.clear")}
          >
            <VStack align="stretch" spacing={0.5}>
              {ACCESS.map((k) => (
                <OptionRow
                  key={k}
                  selected={access === k}
                  onClick={() => {
                    setAccess(k);
                    accessChip.onClose();
                  }}
                >
                  {accessLabel[k]}
                </OptionRow>
              ))}
            </VStack>
          </FilterChip>
          <FilterChip
            icon={ArrowsUpDownIcon}
            label={sortLabel[sort]}
            title={t("admins.sort.title")}
            isOpen={sortChip.isOpen}
            onOpen={sortChip.onOpen}
            onClose={sortChip.onClose}
          >
            <VStack align="stretch" spacing={0.5}>
              {SORTS.map((k) => (
                <OptionRow
                  key={k}
                  selected={sort === k}
                  onClick={() => {
                    setSort(k);
                    sortChip.onClose();
                  }}
                >
                  {sortLabel[k]}
                </OptionRow>
              ))}
            </VStack>
          </FilterChip>
        </Flex>
      </StickyBar>

      <Box pt={3}>
        {isErrorAdmins && !admins.length ? (
          <LoadError message={t("admins.loadError")} onRetry={() => refetchAdmins()} />
        ) : isLoadingAdmins ? (
          <VStack align="stretch" spacing={2}>
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} h="48px" borderRadius="md" />
            ))}
          </VStack>
        ) : admins.length === 0 ? (
          <EmptyState
            compact
            title={t("admins.noAdmins")}
            actionLabel={t("admins.create.submit")}
            onAction={() => openNew()}
          />
        ) : rows.length === 0 &&
          access === "all" &&
          typed &&
          !usernameProblem(typed) &&
          typed.toLowerCase() !== signedIn.toLowerCase() ? (
          // Searched for a name nobody has: the next step is making it.
          <EmptyState
            compact
            title={t("admins.noMatch")}
            actionLabel={t("admins.create.named", { name: typed })}
            onAction={() => openNew(typed)}
          />
        ) : rows.length === 0 ? (
          <EmptyState
            compact
            title={t("admins.noMatch")}
            actionLabel={t("admins.clearFilters")}
            onAction={() => {
              setQuery("");
              setAccess("all");
            }}
          />
        ) : (
          <RankedList
            header={{
              title: t("admins.count", { count: rows.length }),
              value: sort === "traffic" ? t("admins.columns.traffic") : t("admins.columns.users"),
            }}
            items={items}
            valueWidth={sort === "traffic" ? "84px" : "56px"}
          />
        )}
      </Box>

      {admins.length > 0 && (
        <Box mt={8}>
          <Text fontSize="xs" fontWeight="semibold" px={1} pb={2} {...muted}>
            {t("admins.panelWide")}
          </Text>
          <Button
            variant="outline"
            colorScheme="orange"
            w={{ base: "full", md: "auto" }}
            minH="44px"
            // Wraps: the Turkmen label is wider than a 320px phone.
            h="auto"
            py={2}
            whiteSpace="normal"
            leftIcon={<Icon as={ArrowPathIcon} boxSize="16px" />}
            onClick={() => setConfirmAll(true)}
          >
            {t("regen.all")}
          </Button>
        </Box>
      )}

      <AdminSheet
        admin={open}
        traitsOf={(a) => traitsOf(a, domain)}
        onClose={closeSheet}
        onEdit={(a) => setEditing(a.username)}
        onDeleted={closeSheet}
      />
      <CreateAdminSheet
        isOpen={creating && listReady}
        onClose={closeSheet}
        admins={admins}
        signedIn={signedIn}
        prefill={prefill}
        onCreated={onCreated}
        onOpenAdmin={showCreated}
        onCloseComplete={createClosed}
      />
      {/* Looked up on every render, so a saved change shows at once. */}
      <AdminEditDialog
        admin={admins.find((a) => a.username === editing) ?? null}
        onClose={() => setEditing(null)}
      />
      <ConfirmModal
        isOpen={confirmAll}
        onClose={() => setConfirmAll(false)}
        onConfirm={runAll}
        icon={<ArrowPathIcon width={20} height={20} />}
        colorScheme="orange"
        title={t("regen.all")}
        prompt={t("regen.confirmAll")}
        confirmLabel={t("regen.all")}
        typeToConfirm="REGENERATE"
      />
    </Page>
  );
};

export default Admins;
