export type Status =
  | "active"
  | "disabled"
  | "limited"
  | "expired"
  | "on_hold"
  | "error"
  | "connecting"
  | "connected";
export type ProxyKeys = ("vmess" | "vless" | "trojan" | "shadowsocks")[];
export type ProxyType = {
  vmess?: {
    id?: string;
  };
  vless?: {
    id?: string;
    flow?: string;
  };
  trojan?: {
    password?: string;
  };
  shadowsocks?: {
    password?: string;
    method?: string;
  };
};

export type DataLimitResetStrategy =
  | "no_reset"
  | "day"
  | "week"
  | "month"
  | "year";

export type ClientType = "happ" | "micky";

export type UserInbounds = {
  [key: string]: string[];
};
export type User = {
  proxies: ProxyType;
  expire: number | null;
  data_limit: number | null;
  data_limit_reset_strategy: DataLimitResetStrategy;
  on_hold_expire_duration: number | null;
  lifetime_used_traffic: number;
  username: string;
  used_traffic: number;
  status: Status;
  links: string[];
  subscription_url: string;
  inbounds: UserInbounds;
  note: string;
  online_at: string;
  device_count?: number;
  client_type?: ClientType | null;
  device_limit?: number | null;
  // Per-user Outline (Shadowsocks dynamic-key) access gate. Mutable.
  outline_enabled?: boolean | null;
  // Owner admin. Backend always returns this on UserResponse; the
  // dashboard only renders it when the current admin is sudo so
  // non-sudo admins don't see noise (they own every user they can see).
  admin?: {
    id?: number | null;
    username: string;
    is_sudo: boolean;
  } | null;
};

export type UserCreate = Pick<
  User,
  | "inbounds"
  | "proxies"
  | "expire"
  | "data_limit"
  | "data_limit_reset_strategy"
  | "on_hold_expire_duration"
  | "username"
  | "status"
  | "note"
> & {
  client_type?: ClientType | null;
  device_limit?: number | null;
  allowed_hwids?: string[];
  // Per-user Outline access gate. Settable on create and modify.
  outline_enabled?: boolean | null;
};

export type UserApi = {
  discord_webook: string;
  is_sudo: boolean;
  telegram_id: number | string;
  username: string;
  // Whether this admin may post their own subscription announcement.
  can_announce?: boolean;
  // Whether this admin may enable Outline access on their users.
  can_enable_outline?: boolean;
  // RESTRICTION (not a grant): when true this admin may create ONLY Happ
  // device-limited users. The user dialog forces the Happ block on and
  // hides the escape hatches; the backend rejects anything else with 400.
  happ_only?: boolean;
}

export type UseGetUserReturn = {
  userData: UserApi;
  getUserIsPending: boolean;
  getUserIsSuccess: boolean;
  getUserIsError: boolean;
  getUserError: Error | null;
}