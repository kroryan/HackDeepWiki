import type { NextConfig } from "next";

// NOTE: Do not proxy to the backend via `rewrites()` here. Next.js resolves
// rewrite destinations (including any env var interpolated into them) once
// at `next build` time and bakes the result into the standalone output's
// routes manifest — it is NOT re-evaluated when the packaged app later picks
// a different SERVER_BASE_URL/port at runtime (the portable AppImage/.exe
// auto-selects a free backend port, which is rarely the build-time default).
// Every backend-proxied path is instead a real Route Handler under
// src/app/**/route.ts, which reads process.env.SERVER_BASE_URL at request
// time in the running server process, so it tracks the actual runtime port.

const nextConfig: NextConfig = {
  /* config options here */
  output: 'standalone',
  // Optimize build for Docker
  experimental: {
    optimizePackageImports: ['@mermaid-js/mermaid', 'react-syntax-highlighter'],
  },
  // Reduce memory usage during build
  webpack: (config, { isServer }) => {
    if (!isServer) {
      config.resolve.fallback = {
        ...config.resolve.fallback,
        fs: false,
      };
    }
    // Keep Next.js' own splitChunks configuration. Overriding it causes CSS
    // chunks to be emitted as JavaScript dependencies in Next 15.
    return config;
  },
  turbopack: {},
};

export default nextConfig;
