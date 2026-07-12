import { create } from "zustand";

const THEME_STORAGE_KEY = "codex-lb-theme";

export type ThemePreference = "light" | "dark" | "auto";
export type ResolvedTheme = "light" | "dark";

/** @deprecated Use ThemePreference instead */
export type Theme = ResolvedTheme;

type ThemeState = {
  preference: ThemePreference;
  /** The resolved (effective) theme — always "light" | "dark". */
  theme: ResolvedTheme;
  initialized: boolean;
  initializeTheme: () => void;
  setTheme: (pref: ThemePreference) => void;
};

function applyThemeToDocument(theme: ResolvedTheme): void {
  if (typeof document === "undefined") {
    return;
  }
  document.documentElement.classList.toggle("dark", theme === "dark");
}

function getSystemTheme(): ResolvedTheme {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "light";
  }
  try {
    if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
  } catch {
    return "light";
  }
  return "light";
}

function resolveTheme(preference: ThemePreference): ResolvedTheme {
  if (preference === "auto") return getSystemTheme();
  return preference;
}

function readStoredPreference(): ThemePreference | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "auto") {
      return stored;
    }
  } catch {
    return null;
  }
  return null;
}

function writeStoredPreference(preference: ThemePreference): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    /* Storage can be blocked in forwarded or embedded browser contexts. */
  }
}

let mediaQuery: MediaQueryList | null = null;
let mediaListener: ((e: MediaQueryListEvent) => void) | null = null;

function setupSystemThemeListener() {
  cleanupSystemThemeListener();
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
  try {
    mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
  } catch {
    mediaQuery = null;
    return;
  }
  mediaListener = () => {
    const state = useThemeStore.getState();
    if (state.preference === "auto") {
      const resolved = getSystemTheme();
      applyThemeToDocument(resolved);
      useThemeStore.setState({ theme: resolved });
    }
  };
  if (typeof mediaQuery.addEventListener === "function") {
    mediaQuery.addEventListener("change", mediaListener);
  } else {
    mediaQuery.addListener(mediaListener);
  }
}

function cleanupSystemThemeListener() {
  if (mediaQuery && mediaListener) {
    if (typeof mediaQuery.removeEventListener === "function") {
      mediaQuery.removeEventListener("change", mediaListener);
    } else {
      mediaQuery.removeListener(mediaListener);
    }
  }
  mediaQuery = null;
  mediaListener = null;
}

export const useThemeStore = create<ThemeState>((set) => ({
  preference: "auto",
  theme: "light",
  initialized: false,
  initializeTheme: () => {
    const preference = readStoredPreference() ?? "auto";
    const resolved = resolveTheme(preference);
    applyThemeToDocument(resolved);
    writeStoredPreference(preference);
    set({ preference, theme: resolved, initialized: true });
    if (preference === "auto") {
      setupSystemThemeListener();
    }
  },
  setTheme: (pref) => {
    const resolved = resolveTheme(pref);
    applyThemeToDocument(resolved);
    writeStoredPreference(pref);
    set({ preference: pref, theme: resolved, initialized: true });
    if (pref === "auto") {
      setupSystemThemeListener();
    } else {
      cleanupSystemThemeListener();
    }
  },
}));
