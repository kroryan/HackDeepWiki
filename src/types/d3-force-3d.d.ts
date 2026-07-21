// d3-force-3d ships no type definitions of its own (and there's no
// @types/d3-force-3d on DefinitelyTyped) -- minimal ambient shim covering
// only what VulnGraph3DInner actually uses (a 3D-aware collision force).
declare module 'd3-force-3d' {
  export function forceCollide(
    radius?: number | ((node: unknown, i: number, nodes: unknown[]) => number),
  ): unknown;
}
