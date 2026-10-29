import { CircularProgress } from "@chakra-ui/react";
import { Suspense } from "react";
import { createHashRouter, redirect } from "react-router-dom";
import { fetch } from "../service/http";
import { getAuthToken } from "../utils/authStorage";
import { lazyWithReload } from "../utils/lazyWithReload";
import { Dashboard } from "./Dashboard";
import { Login } from "./Login";

const Statistics = lazyWithReload(() => import("./Statistics"));
const Admins = lazyWithReload(() => import("./Admins"));

const fetchAdminLoader = () => {
    return fetch("/admin", {
        headers: {
            Authorization: `Bearer ${getAuthToken()}`,
        },
    });
};

// Sudo only. Anyone else goes back to the users list: a redirect, never a
// throw, since the errorElement (<Login/>) signs the admin out.
const sudoLoader = async () => {
    const admin = await fetchAdminLoader();
    return admin?.is_sudo ? admin : redirect("/");
};

const PageSpinner = () => (
    <div
        style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            minHeight: "100vh",
        }}
    >
        <CircularProgress isIndeterminate />
    </div>
);

export const router = createHashRouter([
    {
        path: "/",
        element: <Dashboard />,
        errorElement: <Login />,
        loader: fetchAdminLoader,
    },
    {
        path: "/statistics/",
        element: (
            <Suspense fallback={<PageSpinner />}>
                <Statistics />
            </Suspense>
        ),
        errorElement: <Login />,
        loader: fetchAdminLoader,
        // The open tab lives in the query string (?tab=apps). Re-running the
        // auth check on every tab tap would make each tap wait on GET /admin,
        // and a failed request would land on <Login/>, which signs the admin out.
        shouldRevalidate: ({ currentUrl, nextUrl }) => currentUrl.pathname !== nextUrl.pathname,
    },
    {
        path: "/admins/",
        element: (
            <Suspense fallback={<PageSpinner />}>
                <Admins />
            </Suspense>
        ),
        errorElement: <Login />,
        loader: sudoLoader,
        // The open admin lives in the query string (?admin=name); same reason
        // as /statistics/ above.
        shouldRevalidate: ({ currentUrl, nextUrl }) => currentUrl.pathname !== nextUrl.pathname,
    },
    {
        path: "/login/",
        element: <Login />,
    },
]);
