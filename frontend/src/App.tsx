import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";

import { AppHeader } from "@/components/layout/app-header";
import { StatusBar } from "@/components/layout/status-bar";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthGate } from "@/features/auth/components/auth-gate";
import { useAuthStore } from "@/features/auth/hooks/use-auth";
import { AccountsPage } from "@/features/accounts/components/accounts-page";
import { ApisPage } from "@/features/apis/components/apis-page";
import { CodexResetForecastPage } from "@/features/codex-reset-forecast/components/codex-reset-forecast-page";
import { DashboardPage } from "@/features/dashboard/components/dashboard-page";
import { MailPage } from "@/features/mail/components/mail-page";
import { NewsPage } from "@/features/news/components/news-page";
import { ProcessesPage } from "@/features/processes/components/processes-page";
import { ScholarPage } from "@/features/scholar/components/scholar-page";
import { getSettings } from "@/features/settings/api";
import { SettingsPage } from "@/features/settings/components/settings-page";
import type { DashboardSettings } from "@/features/settings/schemas";
import { useTimeFormatStore } from "@/hooks/use-time-format";

const SETTINGS_QUERY_KEY = ["settings", "detail"] as const;

function AppLayout() {
  const logout = useAuthStore((state) => state.logout);
  const passwordRequired = useAuthStore((state) => state.passwordRequired);
  const timeFormat = useTimeFormatStore((state) => state.timeFormat);
  const settingsQuery = useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: getSettings,
  });
  const settings = settingsQuery.data;

  return (
    <div className="flex min-h-screen flex-col bg-background pb-10" data-time-format={timeFormat}>
      <AppHeader
        onLogout={() => {
          void logout();
        }}
        showLogout={passwordRequired}
        newsEnabled={settings?.newsRefreshEnabled === true}
        scholarEnabled={settings?.scholarRefreshEnabled === true}
      />
      <main className="mx-auto w-full max-w-[1500px] flex-1 px-4 py-8 sm:px-6">
        <Outlet />
      </main>
      <StatusBar />
    </div>
  );
}

type OptionalSurfaceRouteProps = {
  children: ReactNode;
  isEnabled: (settings: DashboardSettings) => boolean;
};

function OptionalSurfaceRoute({ children, isEnabled }: OptionalSurfaceRouteProps) {
  const settingsQuery = useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: getSettings,
  });

  if (settingsQuery.isLoading) {
    return null;
  }

  if (!settingsQuery.data || !isEnabled(settingsQuery.data)) {
    return <Navigate to="/dashboard" replace />;
  }

  return children;
}

export default function App() {
  return (
    <TooltipProvider>
      <Toaster richColors />
      <AuthGate>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/reset-odds" element={<CodexResetForecastPage />} />
            <Route path="/processes" element={<ProcessesPage />} />
            <Route path="/accounts" element={<AccountsPage />} />
            <Route path="/apis" element={<ApisPage />} />
            <Route path="/mail" element={<MailPage />} />
            <Route
              path="/news"
              element={(
                <OptionalSurfaceRoute isEnabled={(settings) => settings.newsRefreshEnabled}>
                  <NewsPage />
                </OptionalSurfaceRoute>
              )}
            />
            <Route
              path="/scholar"
              element={(
                <OptionalSurfaceRoute isEnabled={(settings) => settings.scholarRefreshEnabled}>
                  <ScholarPage />
                </OptionalSurfaceRoute>
              )}
            />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/firewall" element={<Navigate to="/settings" replace />} />
          </Route>
        </Routes>
      </AuthGate>
    </TooltipProvider>
  );
}
