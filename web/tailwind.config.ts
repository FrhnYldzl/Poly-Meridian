import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Geist Sans",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Consolas",
          "monospace",
        ],
      },
      colors: {
        // Bloomberg-inspired palette — modern dark with amber data accent.
        terminal: {
          bg: "#0a0a0b",        // near-black background
          surface: "#111114",   // panel background
          surfaceAlt: "#16161a",
          border: "#262629",
          borderBright: "#3a3a40",
          dim: "#5c5c63",
          text: "#e5e5e7",
          textBright: "#fafafa",
          amber: "#ff9e0a",     // signature accent
          amberDim: "#b86d00",
          cyan: "#22d3ee",
          green: "#22c55e",
          red: "#ef4444",
          yellow: "#eab308",
          purple: "#a855f7",
        },
      },
      animation: {
        "pulse-slow": "pulse 4s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        blink: "blink 1.6s steps(1) infinite",
      },
      keyframes: {
        blink: {
          "0%, 50%": { opacity: "1" },
          "50.01%, 100%": { opacity: "0" },
        },
      },
    },
  },
  plugins: [],
};

export default config;
