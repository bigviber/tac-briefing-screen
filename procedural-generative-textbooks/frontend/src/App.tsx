import { lazy, Suspense, useEffect } from "react";
import { useRoute } from "./lib/route";
import { useUi } from "./store/uiStore";
import { useCatalog } from "./store/catalogStore";
import { useSettings } from "./store/settingsStore";
import { TopBar } from "./components/TopBar";
import { CommandPalette } from "./components/CommandPalette";
import { Toasts } from "./components/ui/Toasts";
import { ErrorBoundary } from "./components/ui/ErrorBoundary";
import { SkeletonBlock } from "./components/ui/primitives";
import { Dashboard } from "./views/Dashboard";

const Workbench = lazy(() => import("./views/Workbench").then((m) => ({ default: m.Workbench })));
const SettingsDrawer = lazy(() => import("./components/SettingsDrawer").then((m) => ({ default: m.SettingsDrawer })));

export function App() {
  const route = useRoute();
  const settingsOpen = useUi((s) => s.settingsOpen);
  const loadHealth = useCatalog((s) => s.loadHealth);
  const loadSettings = useSettings((s) => s.load);

  useEffect(() => {
    void loadHealth();
    void loadSettings();
    const t = window.setInterval(() => void loadHealth(), 30_000);
    return () => window.clearInterval(t);
  }, [loadHealth, loadSettings]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "k") {
        e.preventDefault();
        const ui = useUi.getState();
        ui.setPalette(!ui.paletteOpen);
      } else if (mod && e.key === ",") {
        e.preventDefault();
        useUi.getState().setSettings(true);
      } else if (e.key === "Escape" && useUi.getState().settingsOpen && !useUi.getState().paletteOpen) {
        useUi.getState().setSettings(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="flex h-full flex-col">
      <TopBar route={route} />
      <main className="min-h-0 flex-1">
        <ErrorBoundary label={route.name === "case" ? "Workbench" : "Dashboard"}>
          {route.name === "dashboard" ? (
            <Dashboard />
          ) : (
            <Suspense fallback={<SkeletonBlock lines={12} />}>
              <Workbench caseId={route.id} />
            </Suspense>
          )}
        </ErrorBoundary>
      </main>
      {settingsOpen && (
        <Suspense fallback={null}>
          <SettingsDrawer />
        </Suspense>
      )}
      <CommandPalette />
      <Toasts />
    </div>
  );
}
