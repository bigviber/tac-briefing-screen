import { useEffect, useState } from "react";

export type Route = { name: "dashboard" } | { name: "case"; id: string };

export function parseHash(hash: string): Route {
  const m = /^#\/case\/([^/?#]+)/.exec(hash);
  return m ? { name: "case", id: decodeURIComponent(m[1]) } : { name: "dashboard" };
}

export function navigate(route: Route): void {
  window.location.hash = route.name === "case" ? `#/case/${encodeURIComponent(route.id)}` : "#/";
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onHash = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}
