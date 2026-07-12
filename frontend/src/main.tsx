import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App.tsx";
import { AppErrorBoundary, DashboardErrorFallback } from "@/components/app-error-boundary";
import { queryClient } from "@/lib/query-client";
import { useThemeStore } from "@/hooks/use-theme";

import "./index.css";

function renderStartupError(error: Error): void {
  const rootElement = document.getElementById("root");
  if (rootElement === null) {
    document.body.textContent = error.message;
    return;
  }

  createRoot(rootElement).render(<DashboardErrorFallback error={error} />);
}

try {
  useThemeStore.getState().initializeTheme();
  const rootElement = document.getElementById("root");
  if (rootElement === null) {
    throw new Error("Dashboard root element is missing");
  }

  createRoot(rootElement).render(
    <StrictMode>
      <AppErrorBoundary>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </AppErrorBoundary>
    </StrictMode>,
  );
} catch (error) {
  console.error("Dashboard startup failed", error);
  renderStartupError(error instanceof Error ? error : new Error("Dashboard startup failed"));
}
