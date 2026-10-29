import {
  Badge,
  Box,
  Button,
  FormLabel,
  HStack,
  Icon,
  IconButton,
  Input,
  InputGroup,
  InputRightElement,
  Select,
  SimpleGrid,
  Switch,
  Text,
  VStack,
} from "@chakra-ui/react";
import {
  CheckIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  DocumentDuplicateIcon,
  ShareIcon,
} from "@heroicons/react/24/outline";
import { zodResolver } from "@hookform/resolvers/zod";
import { FormField, InfoTip, Sheet } from "components/shared";
import { AdminListItem } from "hooks/useAdmins";
import { useCreateAdmin } from "hooks/useAdminManagement";
import { useAppToast } from "hooks/useAppToast";
import { FC, useEffect, useMemo, useRef, useState } from "react";
import CopyToClipboard from "react-copy-to-clipboard";
import { flushSync } from "react-dom";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { z } from "zod";
import {
  codePoints,
  generatePassword,
  isSubscriptionUrl,
  PASSWORD_MIN,
  USERNAME_MAX,
  usernameProblem,
} from "./rules";
import { domainsInUse, FLAGS, host, TraitBadges, useAccessTraits } from "./traits";

const FORM_ID = "create-admin";
const OTHER = "other";
const muted = { color: "gray.600", _dark: { color: "gray.400" } };

type Form = {
  username: string;
  password: string;
  happ_only: boolean;
  can_announce: boolean;
  can_enable_outline: boolean;
  /** A domain in use, "" for the panel default, or OTHER. */
  domain: string;
  otherDomain: string;
  telegram_id: string;
};

// The checks read the taken names through a ref: the list refetches while
// the sheet is open.
const schemaFor = (taken: { current: Set<string> }) =>
  z
    .object({
      username: z.string(),
      password: z.string(),
      happ_only: z.boolean(),
      can_announce: z.boolean(),
      can_enable_outline: z.boolean(),
      domain: z.string(),
      otherDomain: z.string(),
      telegram_id: z.string(),
    })
    .superRefine((v, ctx) => {
      const issue = (path: keyof Form, message: string) =>
        ctx.addIssue({ code: "custom", path: [path], message });
      const name = v.username.trim();
      const bad =
        usernameProblem(name) ??
        // Any case: prod's collation and the backend both ignore it.
        (taken.current.has(name.toLowerCase()) ? "admins.errors.usernameTaken" : null);
      if (bad) issue("username", bad);
      if (codePoints(v.password) < PASSWORD_MIN) issue("password", "admins.errors.passwordMin");
      // Invisible in a shared message, so never what was meant.
      else if (v.password !== v.password.trim()) issue("password", "admins.errors.passwordSpaces");
      if (v.domain === OTHER && !isSubscriptionUrl(v.otherDomain.trim()))
        issue("otherDomain", "admins.errors.url");
      if (!/^\d*$/.test(v.telegram_id)) issue("telegram_id", "admins.errors.telegramDigits");
    });

/** One copyable line of the login card. */
const LoginRow: FC<{ label: string; value: string; badge?: string; first?: boolean }> = ({
  label,
  value,
  badge,
  first,
}) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(id);
  }, [copied]);
  return (
    <HStack px={3} py={2} spacing={2} borderTopWidth={first ? 0 : "1px"}>
      <Box flex="1" minW={0}>
        <HStack spacing={2}>
          <Text fontSize="xs" {...muted}>
            {label}
          </Text>
          {badge && (
            <Badge colorScheme="orange" textTransform="none" fontWeight="medium">
              {badge}
            </Badge>
          )}
        </HStack>
        <Text fontFamily="mono" fontSize="md" wordBreak="break-all">
          {value}
        </Text>
      </Box>
      <CopyToClipboard
        text={value}
        onCopy={(_, ok) => (ok ? setCopied(true) : toast.error(null, t("admins.create.copyError")))}
      >
        <IconButton
          aria-label={copied ? t("usersTable.copied") : t("admins.create.copyField", { field: label })}
          variant="ghost"
          minW="44px"
          h="44px"
          icon={<Icon as={copied ? CheckIcon : DocumentDuplicateIcon} boxSize="18px" />}
        />
      </CopyToClipboard>
    </HStack>
  );
};

type Props = {
  isOpen: boolean;
  onClose: () => void;
  admins: AdminListItem[];
  /** Taken too, though not in the list when an env sudoer. */
  signedIn: string;
  /** A name to start from (the search that found nobody). */
  prefill: string;
  /** Returns whether the login card will show (the sheet may be gone). */
  onCreated: (username: string) => boolean;
  onOpenAdmin: (username: string) => void;
  onCloseComplete?: () => void;
};

/**
 * New admin: a name and a password, with a standard reseller's access
 * already chosen (device-limited, no announcements, no Outline, the domain
 * most admins use) and shown as chips; "Customize" changes it. Ends on the
 * login, ready to share. Sudo admins are made with marzban-cli only.
 */
export const CreateAdminSheet: FC<Props> = ({
  isOpen,
  onClose,
  admins,
  signedIn,
  prefill,
  onCreated,
  onOpenAdmin,
  onCloseComplete,
}) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const create = useCreateAdmin();
  // Rows older than the backend's URL check may hold one it now refuses.
  const domains = useMemo(
    () => domainsInUse(admins).filter((d) => !d || isSubscriptionUrl(d)),
    [admins]
  );
  const taken = useRef(new Set<string>());
  taken.current = new Set([...admins.map((a) => a.username), signedIn].map((n) => n.toLowerCase()));
  const schema = useMemo(() => schemaFor(taken), []);
  const blank = (username: string): Form => ({
    username,
    password: "",
    happ_only: true,
    can_announce: false,
    can_enable_outline: false,
    domain: domains[0] ?? "",
    otherDomain: "",
    telegram_id: "",
  });
  // react-hook-form's default: checked on submit, then live. Not on blur:
  // an error appearing as the field loses focus moves what was tapped from
  // under the finger.
  const form = useForm<Form>({ resolver: zodResolver(schema), defaultValues: blank(prefill) });
  const errors = form.formState.errors;
  const usernameRef = useRef<HTMLInputElement | null>(null);
  const [customize, setCustomize] = useState(false);
  const [created, setCreated] = useState<{ admin: AdminListItem; password: string } | null>(null);

  const openRef = useRef(isOpen);
  openRef.current = isOpen;
  // A late success bringing the sheet back: its card must outlive a close
  // animation that ends just before the reopen renders.
  const reopening = useRef(false);

  const start = (username: string) => {
    form.reset(blank(username));
    setCustomize(false);
    setCreated(null);
  };
  // Opened: fresh defaults, unless a create that outlived the sheet brought
  // it back to show its login.
  useEffect(() => {
    if (isOpen && !created) form.reset(blank(prefill));
    if (isOpen) reopening.current = false;
  }, [isOpen]);
  // Closed: nothing typed or created stays behind, the password included.
  const closed = () => {
    if (openRef.current || reopening.current) return;
    start("");
    if (!create.isLoading) create.reset();
    onCloseComplete?.();
  };

  const accessTraits = useAccessTraits();
  const errText = (m?: string) => m && t(m);
  const v = form.watch();
  // A name already in use is worth saying while it is typed.
  const nameTaken = taken.current.has(v.username.trim().toLowerCase());
  const domainLabel = (d: string) => (d ? host(d) : t("admins.trait.defaultDomain"));
  const summary = accessTraits(
    v,
    v.domain === OTHER
      ? host(v.otherDomain.trim()) || t("admins.create.otherDomain")
      : domainLabel(v.domain)
  );

  const submit = async (values: Form) => {
    const url = values.domain === OTHER ? values.otherDomain.trim() : values.domain;
    try {
      const admin = await create.mutateAsync({
        username: values.username.trim(),
        password: values.password,
        is_sudo: false,
        happ_only: values.happ_only,
        can_announce: values.can_announce,
        can_enable_outline: values.can_enable_outline,
        subscription_url: url || null,
        telegram_id: values.telegram_id ? Number(values.telegram_id) : null,
      });
      setCreated({ admin, password: values.password });
      const shown = onCreated(admin.username);
      if (!openRef.current) {
        if (shown) reopening.current = true;
        // Nowhere to show it: nothing may linger for the next open.
        else start("");
      }
    } catch (e: any) {
      // The rules above are the backend's, so what still fails is a name
      // taken meanwhile; anything else, or a sheet already closed, toasts.
      if (e?.response?.status === 409 && openRef.current)
        form.setError("username", { message: "admins.errors.usernameTaken" }, { shouldFocus: true });
      else toast.error(e, t("admins.create.error"));
    }
  };

  // A bad value can sit in the collapsed Access card: open it to show it
  // (react-hook-form then focuses it).
  const invalid = (errs: typeof errors) => {
    if (errs.otherDomain || errs.telegram_id) setCustomize(true);
  };

  const username = form.register("username");

  const loginText = created
    ? `${t("username")}: ${created.admin.username}\n${t("password")}: ${created.password}`
    : "";
  const canShare = typeof navigator !== "undefined" && typeof navigator.share === "function";
  const share = async () => {
    try {
      await navigator.share({ text: loginText });
    } catch (e: any) {
      // Dismissing the share sheet is not a failure.
      if (e?.name !== "AbortError") toast.error(null, t("admins.create.shareError"));
    }
  };


  const footer = created ? (
    <HStack w="full" spacing={2}>
      {canShare && (
        <Button
          flex="1"
          minH="48px"
          colorScheme="primary"
          leftIcon={<Icon as={ShareIcon} boxSize="18px" />}
          onClick={share}
        >
          {t("admins.create.share")}
        </Button>
      )}
      <CopyToClipboard
        text={loginText}
        onCopy={(_, ok) =>
          ok ? toast.success(t("admins.create.copied")) : toast.error(null, t("admins.create.copyError"))
        }
      >
        <Button
          flex="1"
          minH="48px"
          colorScheme="primary"
          variant={canShare ? "outline" : "solid"}
          leftIcon={<Icon as={DocumentDuplicateIcon} boxSize="18px" />}
        >
          {t("admins.create.copy")}
        </Button>
      </CopyToClipboard>
    </HStack>
  ) : (
    <Button
      type="submit"
      form={FORM_ID}
      w="full"
      minH="48px"
      colorScheme="primary"
      isLoading={create.isLoading}
    >
      {t("admins.create.submit")}
    </Button>
  );

  return (
    <Sheet
      isOpen={isOpen}
      // ✕ and Esc wait for the answer; Back can't, so a late success
      // reopens the sheet (Admins.tsx).
      onClose={create.isLoading ? () => undefined : onClose}
      title={created ? t("admins.create.doneTitle") : t("admins.create.title")}
      footer={footer}
      full
      closeOnOverlayClick={false}
      initialFocusRef={usernameRef}
      onCloseComplete={closed}
    >
      {created ? (
        <VStack align="stretch" spacing={4}>
          <TraitBadges
            traits={accessTraits(created.admin, domainLabel(created.admin.subscription_url ?? ""))}
          />
          <Box borderWidth="1px" borderRadius="lg">
            <LoginRow first label={t("username")} value={created.admin.username} />
            <LoginRow
              label={t("password")}
              value={created.password}
              badge={t("admins.create.shownOnce")}
            />
          </Box>
          <SimpleGrid columns={2} spacing={2}>
            <Button
              variant="outline"
              minH="44px"
              h="auto"
              py={2}
              whiteSpace="normal"
              onClick={() => onOpenAdmin(created.admin.username)}
            >
              {t("admins.create.open")}
            </Button>
            <Button
              variant="outline"
              minH="44px"
              h="auto"
              py={2}
              whiteSpace="normal"
              onClick={() => {
                // Rendered and focused within the tap, or iOS keeps the
                // keyboard down.
                flushSync(() => start(""));
                usernameRef.current?.focus();
              }}
            >
              {t("admins.create.another")}
            </Button>
          </SimpleGrid>
        </VStack>
      ) : (
        <form id={FORM_ID} noValidate onSubmit={form.handleSubmit(submit, invalid)}>
          <VStack align="stretch" spacing={4}>
            <FormField
              label={t("username")}
              error={errText(errors.username?.message) || (nameTaken && t("admins.errors.usernameTaken"))}
            >
              <Input
                h="44px"
                maxLength={USERNAME_MAX}
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                enterKeyHint="next"
                {...username}
                // To the password, not a submit that flags a field not yet reached.
                onKeyDown={(e) => {
                  if (e.key !== "Enter") return;
                  e.preventDefault();
                  form.setFocus("password");
                }}
                ref={(el) => {
                  username.ref(el);
                  usernameRef.current = el;
                }}
              />
            </FormField>
            <FormField label={t("password")} error={errText(errors.password?.message)}>
              <InputGroup>
                {/* In clear: it is someone else's password, shared from this
                    screen, and a typo hidden behind dots goes out with it. */}
                <Input
                  h="44px"
                  pr="6.5rem"
                  fontFamily="mono"
                  enterKeyHint="go"
                  autoComplete="off"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  {...form.register("password")}
                />
                <InputRightElement h="44px" w="auto">
                  <Button
                    variant="ghost"
                    colorScheme="primary"
                    size="sm"
                    h="44px"
                    px={3}
                    borderLeftRadius={0}
                    onClick={() =>
                      form.setValue("password", generatePassword(), {
                        shouldDirty: true,
                        shouldValidate: true,
                      })
                    }
                  >
                    {t("admins.create.generate")}
                  </Button>
                </InputRightElement>
              </InputGroup>
            </FormField>

            <Box borderWidth="1px" borderRadius="lg" px={3} pb={customize ? 3 : 2}>
              <HStack justify="space-between" minH="44px">
                <Text fontSize="sm" fontWeight="semibold">
                  {t("admins.create.access")}
                </Text>
                <Button
                  variant="ghost"
                  colorScheme="primary"
                  size="sm"
                  minH="44px"
                  mr={-2}
                  aria-expanded={customize}
                  rightIcon={<Icon as={customize ? ChevronUpIcon : ChevronDownIcon} boxSize="14px" />}
                  onClick={() => setCustomize((c) => !c)}
                >
                  {customize ? t("admins.create.hide") : t("admins.create.customize")}
                </Button>
              </HStack>
              {!customize ? (
                <TraitBadges traits={summary} />
              ) : (
                <VStack align="stretch" spacing={3}>
                  <Box>
                    {FLAGS.map((f) => (
                      <HStack key={f.key} minH="44px" spacing={1}>
                        <FormLabel
                          htmlFor={`new-admin-${f.key}`}
                          flex="1"
                          minH="44px"
                          display="flex"
                          alignItems="center"
                          m={0}
                          fontSize="sm"
                          fontWeight="normal"
                          cursor="pointer"
                        >
                          {t(f.label)}
                        </FormLabel>
                        <InfoTip label={t(f.label)}>{t(f.help)}</InfoTip>
                        <Switch
                          id={`new-admin-${f.key}`}
                          colorScheme="primary"
                          pl={2}
                          isChecked={v[f.key]}
                          onChange={(e) => form.setValue(f.key, e.target.checked, { shouldDirty: true })}
                        />
                      </HStack>
                    ))}
                  </Box>
                  <FormField label={t("admins.create.domain")}>
                    <Select h="44px" {...form.register("domain")}>
                      {domains.map((d) => (
                        <option key={d} value={d}>
                          {domainLabel(d)}
                        </option>
                      ))}
                      {!domains.includes("") && <option value="">{domainLabel("")}</option>}
                      <option value={OTHER}>{t("admins.create.otherDomain")}</option>
                    </Select>
                  </FormField>
                  {v.domain === OTHER && (
                    <FormField error={errText(errors.otherDomain?.message)}>
                      <Input
                        h="44px"
                        inputMode="url"
                        placeholder="https://sub.example.com"
                        aria-label={t("admins.create.otherDomain")}
                        autoCapitalize="none"
                        autoCorrect="off"
                        spellCheck={false}
                        {...form.register("otherDomain")}
                      />
                    </FormField>
                  )}
                  <FormField
                    label={t("announcements.telegramId")}
                    labelAddon={
                      <Text fontSize="xs" {...muted}>
                        {t("admins.create.optional")}
                      </Text>
                    }
                    error={errText(errors.telegram_id?.message)}
                  >
                    <Input
                      h="44px"
                      inputMode="numeric"
                      maxLength={15}
                      placeholder="123456789"
                      {...form.register("telegram_id")}
                    />
                  </FormField>
                </VStack>
              )}
            </Box>
          </VStack>
        </form>
      )}
    </Sheet>
  );
};
