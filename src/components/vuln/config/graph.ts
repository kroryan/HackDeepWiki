// Force-layout configuration for the 3D graph (react-force-graph-3d).
//
// linkDistance/linkStrength/chargeStrength/collideRadius are applied
// imperatively via fgRef.d3Force(...) in VulnGraph3DInner -- react-force-graph
// only exposes alpha/velocity decay and cooldown as direct props, the rest of
// the d3-force simulation is tuned by reaching into the underlying forces.

export const GRAPH_CONFIG = {
  nodeRelSize: 6,
  cooldownTicks: 300,
  // Matches d3-force's own defaults (and what a legible, non-jittery settle
  // needs): a lower alphaDecay/velocityDecay than this leaves the layout
  // drifting and oscillating for a long time instead of settling decisively.
  d3AlphaDecay: 0.02,
  d3VelocityDecay: 0.4,
  // link force tuning
  linkDistance: 60,
  linkStrength: 0.6,
  chargeStrength: -180,
  collideRadius: 12,
  // glow ring for high-severity nodes
  glowPulseDuration: 1200, // ms
  enableGlowForSeverities: ['CRITICAL', 'HIGH'] as const,
};

export const CAMERA_DISTANCE = 180;
