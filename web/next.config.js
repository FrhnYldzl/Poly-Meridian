/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Static HTML/JS export — the agent's FastAPI process serves the build
  // from /app/static, so the whole dashboard is one Railway service.
  output: "export",
  // SPA-style: each route gets its own index.html.
  trailingSlash: true,
  // Disable next/image optimization — incompatible with static export.
  images: { unoptimized: true },
  // Public env: empty string means "same origin" — fetches go to /api/*
  // relative to the page, which the agent serves at the same host.
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "",
  },
};

module.exports = nextConfig;
