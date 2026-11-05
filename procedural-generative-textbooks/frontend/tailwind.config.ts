import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0b0e14",
        s1: "#11151c",
        s2: "#161b24",
        s3: "#1c2230",
        line: "#232a36",
        fg: "#d7dde8",
        muted: "#8b95a7",
        accent: "#5b8def",
        ok: "#3ecf8e",
        warn: "#f5b74e",
        bad: "#ef5e6e",
        p: {
          af: "#f5b74e",
          os: "#3ecf8e",
          sd: "#b78cff",
          sql: "#5b8def",
          py: "#ff8a5b",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "ui-monospace", "monospace"],
      },
      fontSize: {
        "2xs": ["10px", "14px"],
        xs: ["11px", "16px"],
        sm: ["12px", "18px"],
        base: ["13px", "20px"],
      },
      borderRadius: { DEFAULT: "4px", sm: "2px", md: "4px" },
    },
  },
  plugins: [],
} satisfies Config;
