/** localStorage helpers that never throw (private mode, quota, disabled storage). */
export function loadJson<T extends object>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    // JSON boundary: persisted shapes are owned by this app and versioned by key.
    return raw ? { ...fallback, ...(JSON.parse(raw) as Partial<T>) } : fallback;
  } catch {
    return fallback;
  }
}

export function saveJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable */
  }
}
