import {
  Alert,
  AlertIcon,
  Box,
  Button,
  Collapse,
  Flex,
  FormControl,
  FormErrorMessage,
  FormHelperText,
  FormLabel,
  Grid,
  GridItem,
  HStack,
  IconButton,
  Modal,
  ModalBody,
  ModalCloseButton,
  ModalContent,
  ModalFooter,
  ModalHeader,
  ModalOverlay,
  Select,
  Spinner,
  Switch,
  Text,
  Textarea,
  Tooltip,
  VStack,
  chakra,
  useColorMode,
  useToast,
} from "@chakra-ui/react";
import {
  ChartPieIcon,
  PencilIcon,
  UserPlusIcon,
} from "@heroicons/react/24/outline";
import { zodResolver } from "@hookform/resolvers/zod";
import { resetStrategy } from "constants/UserSettings";
import { FilterUsageType, useDashboard } from "contexts/DashboardContext";
import dayjs from "dayjs";
import { FC, Suspense, useEffect, useState } from "react";
import ReactDatePicker from "react-datepicker";
import { Controller, FormProvider, useForm, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import {
  ClientType,
  ProxyKeys,
  ProxyType,
  User,
  UserCreate,
  UserInbounds,
} from "types/User";
import { relativeExpiryDate } from "utils/dateFormatter";
import { z } from "zod";
import { DeleteIcon } from "./DeleteUserModal";
import { Icon } from "./Icon";
import { Input } from "./Input";
import { RadioGroup } from "./RadioGroup";
import { UsageFilter, createUsageConfig } from "./UsageFilter";
import { ReloadIcon } from "./Filters";
import classNames from "classnames";
import useGetUser from "hooks/useGetUser";
import { lazyWithReload } from "utils/lazyWithReload";
import { AccessPolicySummary } from "./AccessPolicySummary";

// ApexCharts is heavy; lazy-load it so it leaves the main bundle.
const ReactApexChart = lazyWithReload(() => import("react-apexcharts"));

const AddUserIcon = chakra(UserPlusIcon, {
  baseStyle: {
    w: 5,
    h: 5,
  },
});

const EditUserIcon = chakra(PencilIcon, {
  baseStyle: {
    w: 5,
    h: 5,
  },
});

const UserUsageIcon = chakra(ChartPieIcon, {
  baseStyle: {
    w: 5,
    h: 5,
  },
});

export type UserDialogProps = {};

export type FormType = Pick<UserCreate, keyof UserCreate> & {
  selected_proxies: ProxyKeys;
};

const formatUser = (user: User): FormType => {
  return {
    ...user,
    data_limit: user.data_limit
      ? Number((user.data_limit / 1073741824).toFixed(5))
      : user.data_limit,
    on_hold_expire_duration: user.on_hold_expire_duration
      ? Number(user.on_hold_expire_duration / (24 * 60 * 60))
      : user.on_hold_expire_duration,
    selected_proxies: Object.keys(user.proxies) as ProxyKeys,
    client_type: user.client_type ?? null,
    device_limit: user.device_limit ?? null,
    allowed_hwids: [],
    outline_enabled: user.outline_enabled ?? false,
  };
};
// `happOnly` = the current admin may create ONLY device-limited users (Happ or
// Micky; Happ is pre-selected)
// (Admin.happ_only). Pre-selecting client_type here means a restricted admin
// never sees the unlimited state, and the backend would reject it anyway.
const getDefaultValues = (happOnly = false): FormType => {
  const defaultInbounds = Object.fromEntries(useDashboard.getState().inbounds);
  const inbounds: UserInbounds = {};
  for (const key in defaultInbounds) {
    inbounds[key] = defaultInbounds[key].map((i) => i.tag);
  }
  return {
    selected_proxies: Object.keys(defaultInbounds) as ProxyKeys,
    data_limit: null,
    expire: null,
    username: "",
    data_limit_reset_strategy: "no_reset",
    status: "active",
    on_hold_expire_duration: null,
    note: "",
    inbounds,
    proxies: {
      vless: { id: "", flow: "" },
      vmess: { id: "" },
      trojan: { password: "" },
      shadowsocks: { password: "", method: "chacha20-ietf-poly1305" },
    },
    client_type: happOnly ? "happ" : null,
    device_limit: null,
    allowed_hwids: [],
    outline_enabled: false,
  };
};

const mergeProxies = (
  proxyKeys: ProxyKeys,
  proxyType: ProxyType | undefined
): ProxyType => {
  const proxies: ProxyType = proxyKeys.reduce(
    (ac, a) => ({ ...ac, [a]: {} }),
    {}
  );
  if (!proxyType) return proxies;
  proxyKeys.forEach((proxy) => {
    if (proxyType[proxy]) {
      proxies[proxy] = proxyType[proxy];
    }
  });
  return proxies;
};

const baseSchema = {
  username: z.string().min(1, { message: "Required" }),
  selected_proxies: z.array(z.string()).refine((value) => value.length > 0, {
    message: "userDialog.selectOneProtocol",
  }),
  note: z.string().nullable(),
  proxies: z
    .record(z.string(), z.record(z.string(), z.any()))
    .transform((ins) => {
      const deleteIfEmpty = (obj: any, key: string) => {
        if (obj && obj[key] === "") {
          delete obj[key];
        }
      };
      deleteIfEmpty(ins.vmess, "id");
      deleteIfEmpty(ins.vless, "id");
      deleteIfEmpty(ins.trojan, "password");
      deleteIfEmpty(ins.shadowsocks, "password");
      deleteIfEmpty(ins.shadowsocks, "method");
      return ins;
    }),
  data_limit: z
    .string()
    .min(0)
    .or(z.number())
    .nullable()
    .transform((str) => {
      if (str) return Number((parseFloat(String(str)) * 1073741824).toFixed(5));
      return 0;
    }),
  expire: z.number().nullable(),
  data_limit_reset_strategy: z.string(),
  inbounds: z.record(z.string(), z.array(z.string())).transform((ins) => {
    Object.keys(ins).forEach((protocol) => {
      if (Array.isArray(ins[protocol]) && !ins[protocol]?.length)
        delete ins[protocol];
    });
    return ins;
  }),
  // Device-limit policy. Both client_type and device_limit must be set
  // together or both NULL; backend enforces the pair-or-neither rule.
  client_type: z.union([z.literal("happ"), z.literal("micky"), z.null()]).default(null),
  device_limit: z.coerce
    .number()
    .int()
    .min(1)
    .nullable()
    .or(z.literal("").transform(() => null)),
  allowed_hwids: z
    .array(z.string())
    .default([])
    .transform((arr) =>
      arr.map((h) => h.trim()).filter((h) => h.length > 0)
    ),
  // Per-user Outline access gate. Mutable (sent on create and edit).
  outline_enabled: z.boolean().default(false),
};

const schema = z.discriminatedUnion("status", [
  z.object({
    status: z.literal("active"),
    ...baseSchema,
  }),
  z.object({
    status: z.literal("disabled"),
    ...baseSchema,
  }),
  z.object({
    status: z.literal("limited"),
    ...baseSchema,
  }),
  z.object({
    status: z.literal("expired"),
    ...baseSchema,
  }),
  z.object({
    status: z.literal("on_hold"),
    on_hold_expire_duration: z.coerce
      .number()
      .min(0.1, "Required")
      .transform((d) => {
        return d * (24 * 60 * 60);
      }),
    ...baseSchema,
  }),
]);

export const UserDialog: FC<UserDialogProps> = () => {
  const {
    editingUser,
    isCreatingNewUser,
    onCreateUser,
    editUser,
    fetchUserUsage,
    onEditingUser,
    createUser,
    onDeletingUser,
  } = useDashboard();
  const isEditing = !!editingUser;
  const isOpen = isCreatingNewUser || isEditing;
  const { userData, getUserIsSuccess } = useGetUser();
  const isSudo = getUserIsSuccess && !!userData?.is_sudo;
  // Outline access is privileged: sudo or the can_enable_outline grant. The
  // toggle is a one-way latch, so once a user is enabled it shows ON+locked.
  const canEnableOutline =
    isSudo || (getUserIsSuccess && !!userData?.can_enable_outline);
  // RESTRICTION: this admin may create ONLY device-limited users. Sudo
  // bypasses it server-side, so don't restrict a sudo's own dialog either.
  // The Outline toggle needs no extra gating — it already renders only while
  // client_type is unset, and a happ_only admin can never unset it.
  const outlineLocked = isEditing && !!editingUser?.outline_enabled;
  const happOnly = !isSudo && getUserIsSuccess && !!userData?.happ_only;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>("");
  const toast = useToast();
  const { t, i18n } = useTranslation();

  const { colorMode } = useColorMode();

  const [usageVisible, setUsageVisible] = useState(false);
  const handleUsageToggle = () => {
    setUsageVisible((current) => !current);
  };

  const form = useForm<FormType>({
    defaultValues: getDefaultValues(),
    resolver: zodResolver(schema),
  });

  // GET /admin resolves after mount, so happOnly can flip to true *after* the
  // form was initialised with client_type=null. Force it on for a restricted
  // admin creating a user — otherwise the dialog would open in a state the
  // backend rejects with 400.
  useEffect(() => {
    if (happOnly && !isEditing && isOpen) {
      form.setValue("client_type", "happ", { shouldDirty: false });
    }
  }, [happOnly, isEditing, isOpen]);

  useEffect(
    () =>
      useDashboard.subscribe(
        (state) => state.inbounds,
        () => {
          form.reset(getDefaultValues(happOnly));
        }
      ),
    []
  );

  const [
    dataLimit,
    userStatus,
    clientType,
    deviceLimit,
    allowedHwids,
    outlineEnabled,
  ] = useWatch({
    control: form.control,
    name: [
      "data_limit",
      "status",
      "client_type",
      "device_limit",
      "allowed_hwids",
      "outline_enabled",
    ],
  });

  const usageTitle = t("userDialog.total");
  const [usage, setUsage] = useState(createUsageConfig(colorMode, usageTitle));
  const [usageFilter, setUsageFilter] = useState("1m");
  const fetchUsageWithFilter = (query: FilterUsageType) => {
    fetchUserUsage(editingUser!, query).then((data: any) => {
      const labels = [];
      const series = [];
      for (const key in data.usages) {
        series.push(data.usages[key].used_traffic);
        labels.push(data.usages[key].node_name);
      }
      setUsage(createUsageConfig(colorMode, usageTitle, series, labels));
    });
  };

  useEffect(() => {
    if (editingUser) {
      form.reset(formatUser(editingUser));

      fetchUsageWithFilter({
        start: dayjs().utc().subtract(30, "day").format("YYYY-MM-DDTHH:00:00"),
      });
    }
  }, [editingUser]);

  const submit = (values: FormType) => {
    setLoading(true);
    const methods = { edited: editUser, created: createUser };
    const method = isEditing ? "edited" : "created";
    setError(null);

    const { selected_proxies, ...rest } = values;

    let body: UserCreate = {
      ...rest,
      data_limit: values.data_limit,
      proxies: mergeProxies(selected_proxies, values.proxies),
      data_limit_reset_strategy:
        values.data_limit && values.data_limit > 0
          ? values.data_limit_reset_strategy
          : "no_reset",
      status:
        values.status === "active" ||
          values.status === "disabled" ||
          values.status === "on_hold"
          ? values.status
          : "active",
    };

    methods[method](body)
      .then(() => {
        toast({
          title: t(
            isEditing ? "userDialog.userEdited" : "userDialog.userCreated",
            { username: values.username }
          ),
          status: "success",
          isClosable: true,
          position: "top",
          duration: 3000,
        });
        onClose();
      })
      .catch((err) => {
        if (err?.response?.status === 409 || err?.response?.status === 400)
          setError(err?.response?._data?.detail);
        if (err?.response?.status === 422) {
          Object.keys(err.response._data.detail).forEach((key) => {
            setError(err?.response._data.detail[key] as string);
            form.setError(
              key as "proxies" | "username" | "data_limit" | "expire",
              {
                type: "custom",
                message: err.response._data.detail[key],
              }
            );
          });
        }
      })
      .finally(() => {
        setLoading(false);
      });
  };

  const onClose = () => {
    form.reset(getDefaultValues(happOnly));
    onCreateUser(false);
    onEditingUser(null);
    setError(null);
    setUsageVisible(false);
    setUsageFilter("1m");
  };

  const handleResetUsage = () => {
    useDashboard.setState({ resetUsageUser: editingUser });
  };

  const handleRevokeSubscription = () => {
    useDashboard.setState({ revokeSubscriptionUser: editingUser });
  };

  const disabled = loading;
  const isOnHold = userStatus === "on_hold";

  const [randomUsernameLoading, setrandomUsernameLoading] = useState(false);

  const createRandomUsername = (): string => {
    setrandomUsernameLoading(true);
    let result = "";
    const characters =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    const charactersLength = characters.length;
    let counter = 0;
    while (counter < 6) {
      result += characters.charAt(Math.floor(Math.random() * charactersLength));
      counter += 1;
    }
    return result;
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} size="2xl">
      <ModalOverlay bg="blackAlpha.300" backdropFilter="blur(10px)" />
      <FormProvider {...form}>
        <ModalContent mx="3">
          <form onSubmit={form.handleSubmit(submit)}>
            <ModalHeader pt={6}>
              <HStack gap={2}>
                <Icon color="primary">
                  {isEditing ? (
                    <EditUserIcon color="white" />
                  ) : (
                    <AddUserIcon color="white" />
                  )}
                </Icon>
                <Text fontWeight="semibold" fontSize="lg">
                  {isEditing
                    ? t("userDialog.editUserTitle")
                    : t("createNewUser")}
                </Text>
              </HStack>
            </ModalHeader>
            <ModalCloseButton mt={3} disabled={disabled} />
            <ModalBody>
              <Grid
                templateColumns={{
                  base: "repeat(1, 1fr)",
                  md: "repeat(2, 1fr)",
                }}
                gap={3}
              >
                <GridItem>
                  <VStack justifyContent="space-between">
                    <Flex
                      flexDirection="column"
                      gridAutoRows="min-content"
                      w="full"
                    >
                      <Flex flexDirection="row" w="full" gap={2}>
                        <FormControl mb={"10px"}>
                          <FormLabel>
                            <Flex gap={2} alignItems={"center"}>
                              {t("username")}
                              {!isEditing && (
                                <ReloadIcon
                                  cursor={"pointer"}
                                  className={classNames({
                                    "animate-spin": randomUsernameLoading,
                                  })}
                                  onClick={() => {
                                    const randomUsername =
                                      createRandomUsername();
                                    form.setValue("username", randomUsername);
                                    setTimeout(() => {
                                      setrandomUsernameLoading(false);
                                    }, 350);
                                  }}
                                />
                              )}
                            </Flex>
                          </FormLabel>
                          <HStack>
                            <Input
                              size="sm"
                              type="text"
                              borderRadius="6px"
                              error={form.formState.errors.username?.message}
                              disabled={disabled || isEditing}
                              {...form.register("username")}
                            />
                            {isEditing && (
                              <HStack px={1}>
                                <Controller
                                  name="status"
                                  control={form.control}
                                  render={({ field }) => {
                                    return (
                                      <Tooltip
                                        placement="top"
                                        label={"status: " + t(`status.${field.value}`)}
                                        textTransform="capitalize"
                                      >
                                        <Box>
                                          <Switch
                                            colorScheme="primary"
                                            isChecked={field.value === "active"}
                                            onChange={(e) => {
                                              if (e.target.checked) {
                                                field.onChange("active");
                                              } else {
                                                field.onChange("disabled");
                                              }
                                            }}
                                          />
                                        </Box>
                                      </Tooltip>
                                    );
                                  }}
                                />
                              </HStack>
                            )}
                          </HStack>
                        </FormControl>
                        {!isEditing && (
                          <FormControl flex="1">
                            <FormLabel whiteSpace={"nowrap"}>
                              {t("userDialog.onHold")}
                            </FormLabel>
                            <Controller
                              name="status"
                              control={form.control}
                              render={({ field }) => {
                                const status = field.value;
                                return (
                                  <>
                                    {status ? (
                                      <Switch
                                        colorScheme="primary"
                                        isChecked={status === "on_hold"}
                                        onChange={(e) => {
                                          if (e.target.checked) {
                                            field.onChange("on_hold");
                                          } else {
                                            field.onChange("active");
                                          }
                                        }}
                                      />
                                    ) : (
                                      ""
                                    )}
                                  </>
                                );
                              }}
                            />
                          </FormControl>
                        )}
                      </Flex>
                      <FormControl mb={"10px"}>
                        <FormLabel>{t("userDialog.dataLimit")}</FormLabel>
                        <Controller
                          control={form.control}
                          name="data_limit"
                          render={({ field }) => {
                            return (
                              <Input
                                endAdornment="GB"
                                type="number"
                                size="sm"
                                borderRadius="6px"
                                onChange={field.onChange}
                                disabled={disabled}
                                error={
                                  form.formState.errors.data_limit?.message
                                }
                                value={field.value ? String(field.value) : ""}
                              />
                            );
                          }}
                        />
                      </FormControl>
                      <Collapse
                        in={!!(dataLimit && dataLimit > 0)}
                        animateOpacity
                        style={{ width: "100%" }}
                      >
                        <FormControl height="66px">
                          <FormLabel>
                            {t("userDialog.periodicUsageReset")}
                          </FormLabel>
                          <Controller
                            control={form.control}
                            name="data_limit_reset_strategy"
                            render={({ field }) => {
                              return (
                                <Select
                                  size="sm"
                                  {...field}
                                  disabled={disabled}
                                  bg={disabled ? "gray.100" : "transparent"}
                                  _dark={{
                                    bg: disabled ? "gray.600" : "transparent",
                                  }}
                                  sx={{
                                    option: {
                                      backgroundColor: colorMode === "dark" ? "#222C3B" : "white"
                                    }
                                  }}
                                >
                                  {resetStrategy.map((s) => {
                                    return (
                                      <option key={s.value} value={s.value}>
                                        {t(
                                          "userDialog.resetStrategy" + s.title
                                        )}
                                      </option>
                                    );
                                  })}
                                </Select>
                              );
                            }}
                          />
                        </FormControl>
                      </Collapse>

                      <FormControl mb={"10px"}>
                        <FormLabel>
                          {isOnHold
                            ? t("userDialog.onHoldExpireDuration")
                            : t("userDialog.expiryDate")}
                        </FormLabel>

                        {isOnHold && (
                          <Controller
                            control={form.control}
                            name="on_hold_expire_duration"
                            render={({ field }) => {
                              return (
                                <Input
                                  endAdornment="Days"
                                  type="number"
                                  size="sm"
                                  borderRadius="6px"
                                  onChange={(on_hold) => {
                                    form.setValue("expire", null);
                                    field.onChange({
                                      target: {
                                        value: on_hold,
                                      },
                                    });
                                  }}
                                  disabled={disabled}
                                  error={
                                    form.formState.errors
                                      .on_hold_expire_duration?.message
                                  }
                                  value={field.value ? String(field.value) : ""}
                                />
                              );
                            }}
                          />
                        )}
                        {!isOnHold && (
                          <Controller
                            name="expire"
                            control={form.control}
                            render={({ field }) => {
                              function createDateAsUTC(num: number) {
                                return dayjs(
                                  dayjs(num * 1000).utc()
                                  // .format("MMMM D, YYYY") // exception with: dayjs.locale(lng);
                                ).toDate();
                              }
                              const { status, time } = relativeExpiryDate(
                                field.value
                              );
                              return (
                                <>
                                  <ReactDatePicker
                                    locale={i18n.language.toLocaleLowerCase()}
                                    dateFormat={t("dateFormat")}
                                    minDate={new Date()}
                                    selected={
                                      field.value
                                        ? createDateAsUTC(field.value)
                                        : undefined
                                    }
                                    onChange={(date: Date) => {
                                      form.setValue(
                                        "on_hold_expire_duration",
                                        null
                                      );
                                      field.onChange({
                                        target: {
                                          value: date
                                            ? dayjs(
                                              dayjs(date)
                                                .set("hour", 23)
                                                .set("minute", 59)
                                                .set("second", 59)
                                            )
                                              .utc()
                                              .valueOf() / 1000
                                            : 0,
                                          name: "expire",
                                        },
                                      });
                                    }}
                                    customInput={
                                      <Input
                                        size="sm"
                                        type="text"
                                        borderRadius="6px"
                                        clearable
                                        disabled={disabled}
                                        error={
                                          form.formState.errors.expire?.message
                                        }
                                      />
                                    }
                                  />
                                  {field.value ? (
                                    <FormHelperText>
                                      {t(status, { time: time })}
                                    </FormHelperText>
                                  ) : (
                                    ""
                                  )}
                                </>
                              );
                            }}
                          />
                        )}
                      </FormControl>

                      {isEditing ? (
                        <Box mb={"10px"}>
                          <AccessPolicySummary
                            form={form}
                            editingUser={editingUser}
                            disabled={disabled}
                            canEnableOutline={canEnableOutline}
                            canResetDevices={isSudo}
                          />
                        </Box>
                      ) : (
                        <>
                      <FormControl mb={"10px"}>
                        <FormLabel>{t("userDialog.deviceLimit")}</FormLabel>
                        <VStack alignItems="stretch" spacing={2}>
                          <Select
                            size="sm"
                            disabled={disabled || isEditing}
                            value={clientType ?? ""}
                            onChange={(e) => {
                              const v = e.target.value as ClientType | "";
                              form.setValue(
                                "client_type",
                                v === "" ? null : v,
                                { shouldDirty: true }
                              );
                              // Pre-registered HWIDs are Happ-only; drop any
                              // typed under Happ when switching away from it.
                              if (v !== "happ") {
                                form.setValue("allowed_hwids", [], {
                                  shouldDirty: true,
                                });
                              }
                              if (v === "") {
                                form.setValue("device_limit", null, {
                                  shouldDirty: true,
                                });
                              } else {
                                // outline is mutually exclusive with a
                                // device limit
                                form.setValue("outline_enabled", false, {
                                  shouldDirty: true,
                                });
                              }
                            }}
                          >
                            {/* A happ_only admin gets no unlimited option —
                                the backend rejects it with 400 anyway. */}
                            {!happOnly && (
                              <option value="">
                                {t("userDialog.noLimit")}
                              </option>
                            )}
                            <option value="happ">
                              {t("userDialog.clientHapp")}
                            </option>
                            <option value="micky">
                              {t("userDialog.clientMicky")}
                            </option>
                          </Select>
                          {clientType ? (
                            <Input
                              size="sm"
                              type="number"
                              min={1}
                              max={20}
                              placeholder={t("userDialog.deviceLimitCount")}
                              disabled={disabled || isEditing}
                              value={
                                deviceLimit == null ? "" : String(deviceLimit)
                              }
                              onChange={(e) => {
                                const raw = e.target.value;
                                const n =
                                  raw === "" ? null : parseInt(raw, 10);
                                const clean =
                                  n !== null && Number.isFinite(n) && n >= 1
                                    ? n
                                    : null;
                                form.setValue("device_limit", clean, {
                                  shouldDirty: true,
                                });
                                const list = allowedHwids || [];
                                if (clean != null && list.length > clean) {
                                  form.setValue(
                                    "allowed_hwids",
                                    list.slice(0, clean),
                                    { shouldDirty: true }
                                  );
                                }
                              }}
                            />
                          ) : null}
                        </VStack>
                        <FormHelperText>
                          {happOnly
                            ? t("userDialog.happOnlyHelp")
                            : t("userDialog.deviceLimitHelp")}
                        </FormHelperText>
                      </FormControl>

                      {!isEditing && clientType === "happ" && deviceLimit ? (
                        <FormControl mb={"10px"}>
                          <FormLabel>{t("userDialog.allowedHwids")}</FormLabel>
                          <VStack alignItems="stretch" spacing={2}>
                            {Array.from({ length: deviceLimit }).map((_, i) => {
                              const value = (allowedHwids || [])[i] ?? "";
                              return (
                                <HStack key={i} spacing={2} align="center">
                                  <Box
                                    minW="22px"
                                    h="22px"
                                    px={1.5}
                                    borderRadius="md"
                                    bg={value ? "primary.500" : "gray.200"}
                                    _dark={{
                                      bg: value ? "primary.500" : "gray.600",
                                    }}
                                    color={value ? "white" : "gray.600"}
                                    fontSize="xs"
                                    fontWeight="bold"
                                    display="grid"
                                    placeItems="center"
                                  >
                                    {i + 1}
                                  </Box>
                                  <Input
                                    size="sm"
                                    fontFamily="mono"
                                    fontSize="13px"
                                    disabled={disabled}
                                    placeholder={t(
                                      "userDialog.hwidPlaceholder"
                                    )}
                                    value={value}
                                    onChange={(e) => {
                                      const current = allowedHwids || [];
                                      const next = [...current];
                                      while (next.length <= i) next.push("");
                                      next[i] = e.target.value;
                                      form.setValue("allowed_hwids", next, {
                                        shouldDirty: true,
                                      });
                                    }}
                                  />
                                </HStack>
                              );
                            })}
                          </VStack>
                          <FormHelperText>
                            {t("userDialog.allowedHwidsHelp")}
                          </FormHelperText>
                        </FormControl>
                      ) : null}

                      {canEnableOutline && !clientType && (
                        <FormControl mb={"10px"}>
                          <HStack
                            justifyContent="space-between"
                            alignItems="center"
                          >
                            <FormLabel mb={0}>
                              {t("userDialog.outlineAccess")}
                            </FormLabel>
                            <Switch
                              colorScheme="primary"
                              isChecked={!!outlineEnabled}
                              isDisabled={disabled || outlineLocked}
                              onChange={(e) => {
                                form.setValue(
                                  "outline_enabled",
                                  e.target.checked,
                                  { shouldDirty: true }
                                );
                              }}
                            />
                          </HStack>
                          <FormHelperText>
                            {outlineLocked
                              ? t("userDialog.outlineAccessLocked")
                              : t("userDialog.outlineAccessHelp")}
                          </FormHelperText>
                        </FormControl>
                      )}
                        </>
                      )}

                      <FormControl
                        mb={"10px"}
                        isInvalid={!!form.formState.errors.note}
                      >
                        <FormLabel>{t("userDialog.note")}</FormLabel>
                        <Textarea {...form.register("note")} />
                        <FormErrorMessage>
                          {form.formState.errors?.note?.message}
                        </FormErrorMessage>
                      </FormControl>
                    </Flex>
                    {error && (
                      <Alert
                        status="error"
                        display={{ base: "none", md: "flex" }}
                      >
                        <AlertIcon />
                        {error}
                      </Alert>
                    )}
                  </VStack>
                </GridItem>
                <GridItem>
                  <FormControl
                    isInvalid={
                      !!form.formState.errors.selected_proxies?.message
                    }
                  >
                    <FormLabel>{t("userDialog.protocols")}</FormLabel>
                    <Controller
                      control={form.control}
                      name="selected_proxies"
                      render={({ field }) => {
                        return (
                          <RadioGroup
                            list={[
                              {
                                title: "vmess",
                                description: t("userDialog.vmessDesc"),
                              },
                              {
                                title: "vless",
                                description: t("userDialog.vlessDesc"),
                              },
                              {
                                title: "trojan",
                                description: t("userDialog.trojanDesc"),
                              },
                              {
                                title: "shadowsocks",
                                description: t("userDialog.shadowsocksDesc"),
                              },
                            ]}
                            disabled={disabled}
                            {...field}
                          />
                        );
                      }}
                    />
                    <FormErrorMessage>
                      {t(
                        form.formState.errors.selected_proxies
                          ?.message as string
                      )}
                    </FormErrorMessage>
                  </FormControl>
                </GridItem>
                {isEditing && usageVisible && (
                  <GridItem pt={6} colSpan={{ base: 1, md: 2 }}>
                    <VStack gap={4}>
                      <UsageFilter
                        defaultValue={usageFilter}
                        onChange={(filter, query) => {
                          setUsageFilter(filter);
                          fetchUsageWithFilter(query);
                        }}
                      />
                      <Box
                        width={{ base: "100%", md: "70%" }}
                        justifySelf="center"
                      >
                        <Suspense fallback={<Spinner />}>
                          <ReactApexChart
                            options={usage.options}
                            series={usage.series}
                            type="donut"
                          />
                        </Suspense>
                      </Box>
                    </VStack>
                  </GridItem>
                )}
              </Grid>
              {error && (
                <Alert
                  mt="3"
                  status="error"
                  display={{ base: "flex", md: "none" }}
                >
                  <AlertIcon />
                  {error}
                </Alert>
              )}
            </ModalBody>
            <ModalFooter mt="3">
              <HStack
                justifyContent="space-between"
                w="full"
                gap={3}
                flexDirection={{
                  base: "column",
                  sm: "row",
                }}
              >
                <HStack
                  justifyContent="flex-start"
                  w={{
                    base: "full",
                    sm: "unset",
                  }}
                >
                  {isEditing && (
                    <>
                      <Tooltip label={t("delete")} placement="top">
                        <IconButton
                          aria-label={t("delete")}
                          size="sm"
                          onClick={() => {
                            onDeletingUser(editingUser);
                            onClose();
                          }}
                        >
                          <DeleteIcon />
                        </IconButton>
                      </Tooltip>
                      <Tooltip label={t("userDialog.usage")} placement="top">
                        <IconButton
                          aria-label={t("userDialog.usage")}
                          size="sm"
                          onClick={handleUsageToggle}
                        >
                          <UserUsageIcon />
                        </IconButton>
                      </Tooltip>
                      <Button onClick={handleResetUsage} size="sm">
                        {t("userDialog.resetUsage")}
                      </Button>
                      <Button onClick={handleRevokeSubscription} size="sm">
                        {t("userDialog.revokeSubscription")}
                      </Button>
                    </>
                  )}
                </HStack>
                <HStack
                  w="full"
                  maxW={{ md: "50%", base: "full" }}
                  justify="end"
                >
                  <Button
                    type="submit"
                    size="sm"
                    px="8"
                    colorScheme="primary"
                    leftIcon={loading ? <Spinner size="xs" /> : undefined}
                    disabled={disabled}
                  >
                    {isEditing ? t("userDialog.editUser") : t("createUser")}
                  </Button>
                </HStack>
              </HStack>
            </ModalFooter>
          </form>
        </ModalContent>
      </FormProvider>
    </Modal>
  );
};
