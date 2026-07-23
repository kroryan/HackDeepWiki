import React, { useEffect, useRef, useState } from 'react';
import mermaid from 'mermaid';
import { useTheme } from 'next-themes';
import { useLanguage } from '@/contexts/LanguageContext';
import { normalizeMermaidChart } from '@/utils/mermaid';
// We'll use dynamic import for svg-pan-zoom

// Initialize mermaid with defaults - Japanese aesthetic
mermaid.initialize({
  startOnLoad: true,
  theme: 'neutral',
  securityLevel: 'loose',
  suppressErrorRendering: true,
  logLevel: 'error',
  maxTextSize: 100000, // Increase text size limit
  htmlLabels: true,
  flowchart: {
    htmlLabels: true,
    curve: 'basis',
    nodeSpacing: 60,
    rankSpacing: 60,
    padding: 20,
  },
  themeCSS: `
    /* Japanese aesthetic styles for all diagrams */
    .node rect, .node circle, .node ellipse, .node polygon, .node path {
      fill: #f8f4e6;
      stroke: #d7c4bb;
      stroke-width: 1px;
    }
    .edgePath .path {
      stroke: #9b7cb9;
      stroke-width: 1.5px;
    }
    .edgeLabel {
      background-color: transparent;
      color: #333333;
      p {
        background-color: transparent !important;
      }
    }
    .label {
      color: #333333;
    }
    .cluster rect {
      fill: #f8f4e6;
      stroke: #d7c4bb;
      stroke-width: 1px;
    }

    /* Sequence diagram specific styles */
    .actor {
      fill: #f8f4e6;
      stroke: #d7c4bb;
      stroke-width: 1px;
    }
    text.actor {
      fill: #333333;
      stroke: none;
    }
    .messageText {
      fill: #333333;
      stroke: none;
    }
    .messageLine0, .messageLine1 {
      stroke: #9b7cb9;
    }
    .noteText {
      fill: #333333;
    }

    /* Dark mode overrides - will be applied with data-theme="dark" */
    [data-theme="dark"] .node rect,
    [data-theme="dark"] .node circle,
    [data-theme="dark"] .node ellipse,
    [data-theme="dark"] .node polygon,
    [data-theme="dark"] .node path {
      fill: #222222;
      stroke: #5d4037;
    }
    [data-theme="dark"] .edgePath .path {
      stroke: #9370db;
    }
    [data-theme="dark"] .edgeLabel {
      background-color: transparent;
      color: #f0f0f0;
    }
    [data-theme="dark"] .label {
      color: #f0f0f0;
    }
    [data-theme="dark"] .cluster rect {
      fill: #222222;
      stroke: #5d4037;
    }
    [data-theme="dark"] .flowchart-link {
      stroke: #9370db;
    }

    /* Dark mode sequence diagram overrides */
    [data-theme="dark"] .actor {
      fill: #222222;
      stroke: #5d4037;
    }
    [data-theme="dark"] text.actor {
      fill: #f0f0f0;
      stroke: none;
    }
    [data-theme="dark"] .messageText {
      fill: #f0f0f0;
      stroke: none;
      font-weight: 500;
    }
    [data-theme="dark"] .messageLine0, [data-theme="dark"] .messageLine1 {
      stroke: #9370db;
      stroke-width: 1.5px;
    }
    [data-theme="dark"] .noteText {
      fill: #f0f0f0;
    }
    /* Additional styles for sequence diagram text */
    [data-theme="dark"] #sequenceNumber {
      fill: #f0f0f0;
    }
    [data-theme="dark"] text.sequenceText {
      fill: #f0f0f0;
      font-weight: 500;
    }
    [data-theme="dark"] text.loopText, [data-theme="dark"] text.loopText tspan {
      fill: #f0f0f0;
    }
    /* Add a subtle background to message text for better readability */
    [data-theme="dark"] .messageText, [data-theme="dark"] text.sequenceText {
      paint-order: stroke;
      stroke: #1a1a1a;
      stroke-width: 2px;
      stroke-linecap: round;
      stroke-linejoin: round;
    }

    /* Force text elements to be properly colored.
       With htmlLabels enabled, node/edge labels are rendered as HTML divs inside
       foreignObject, so they use CSS color (inherited from the page), not SVG fill.
       On light node fills (#f8f4e6) the inherited page foreground (often white/light)
       makes the label text invisible. Set both fill (for SVG text) and color (for
       HTML labels) with !important so labels are always legible on the node fill. */
    text[text-anchor][dominant-baseline],
    text[text-anchor][alignment-baseline],
    .nodeLabel,
    .edgeLabel,
    .label,
    .label foreignObject div,
    .label foreignObject span,
    text {
      fill: #2b2b2b !important;
      color: #2b2b2b !important;
    }
    /* Edge labels sit on the transparent background, keep them dark too */
    .edgeLabel foreignObject div,
    .edgeLabel foreignObject span,
    .edgeLabel span {
      color: #2b2b2b !important;
      background-color: transparent !important;
    }

    [data-theme="dark"] text[text-anchor][dominant-baseline],
    [data-theme="dark"] text[text-anchor][alignment-baseline],
    [data-theme="dark"] .nodeLabel,
    [data-theme="dark"] .edgeLabel,
    [data-theme="dark"] .label,
    [data-theme="dark"] .label foreignObject div,
    [data-theme="dark"] .label foreignObject span,
    [data-theme="dark"] text {
      fill: #f0f0f0 !important;
      color: #f0f0f0 !important;
    }
    [data-theme="dark"] .edgeLabel foreignObject div,
    [data-theme="dark"] .edgeLabel foreignObject span,
    [data-theme="dark"] .edgeLabel span {
      color: #f0f0f0 !important;
    }

    /* Add clickable element styles with subtle transitions */
    .clickable {
      transition: all 0.3s ease;
    }
    .clickable:hover {
      transform: scale(1.03);
      cursor: pointer;
    }
    .clickable:hover > * {
      filter: brightness(0.95);
    }
  `,
  fontFamily: 'var(--font-geist-sans), var(--font-serif), sans-serif',
  fontSize: 12,
});

/**
 * Force every Mermaid diagram to be legible in BOTH themes, no matter the
 * mermaid version or diagram type.
 *
 * The rule is uniform per theme, which is what makes it safe:
 *   - Light mode: white canvas, white box fills, ALL text black.
 *   - Dark mode: soft dark canvas, slightly lighter dark box fills, ALL text
 *     light. Because every background in the diagram (canvas AND boxes) is
 *     dark, the single light text color is legible everywhere — there is no
 *     spot where light text could land on a light fill.
 *
 * Mermaid draws node/edge labels as HTML inside <foreignObject>, which inherit
 * CSS `color` from the page, while pure-SVG labels use `fill`. Rather than rely
 * on CSS specificity or themeCSS (both unreliable across diagram types), we walk
 * the rendered SVG and set inline styles directly on the DOM elements — inline
 * styles set via JS beat everything except `!important`, which mermaid does not
 * emit.
 */
function forceMermaidReadable(root: HTMLElement | null) {
  if (!root) return;
  const svg = root.querySelector('svg');
  if (!svg) return;

  const isDark =
    document.documentElement.getAttribute('data-theme') === 'dark' ||
    document.documentElement.classList.contains('dark');

  // Uniform palette per theme (dark values: muted navy, easy on the eyes).
  const canvas = isDark ? '#16161e' : '#ffffff';
  const boxFill = isDark ? '#24283b' : '#ffffff';
  const boxStroke = isDark ? '#565f89' : '#d7c4bb';
  const textColor = isDark ? '#e8e8f2' : '#000000';
  const lineColor = isDark ? '#a9b1d6' : '#9b7cb9';

  // 0. Canvas for the WHOLE diagram, so text outside boxes (sequence message
  //    labels, arrow annotations, loop labels…) always sits on a known
  //    background. Scoped to the diagram SVG only.
  svg.style.backgroundColor = canvas;

  // Detect sequence diagrams by their mermaid-specific SVG classes. Sequence
  // diagrams render labels as plain SVG <text> sitting on the dark canvas /
  // dark actor boxes (their .actor boxes are NOT matched by the generic
  // `.node rect` rule in step 2, so they stay dark in dark mode). The fdwStyle
  // injected into every SVG forces ALL <text> to near-black (#1a1a1a) with
  // !important, which beats the non-important inline fill set below — so without
  // an override, sequence text ends up dark-on-dark and unreadable. For sequence
  // diagrams in dark mode we therefore set the fill via inline !important
  // (style.setProperty priority='important'), which beats the injected
  // stylesheet's !important rule (inline !important > author stylesheet
  // !important). Pure white for maximum contrast, as requested. Every other
  // diagram type keeps its current behavior (dark text, legible on the light
  // cream node fills that fdwStyle also forces).
  const isSequence = !!svg.querySelector(
    '.messageText, .actor-line, text.actor, #sequenceNumber, .messageLine0, .messageLine1, .noteText, .labelBox',
  );
  const forceWhiteText = isDark && isSequence;

  // 1. Every SVG <text> element (sequence diagrams, actor names, state labels,
  //    axis text, etc.) -> the theme's text color.
  //
  //    For sequence diagrams in dark mode we must also force the <tspan>
  //    children, not just the <text>. Mermaid's own themeCSS sets fill directly
  //    on the tspans (`.noteText > tspan`, `.labelText > tspan`,
  //    `.loopText > tspan` …) to a gray/note color. A directly-specified author
  //    rule on the tspan beats the fill INHERITED from the parent <text>, so
  //    setting white only on <text> left note/loop/label text gray on the dark
  //    boxes — unreadable. Setting inline !important white on the tspans too
  //    beats mermaid's tspan rule (no !important) and the injected fdwStyle
  //    (#1a1a1a !important only matches <text>, not <tspan>). Message text has
  //    no `> tspan` rule so it already inherited white correctly.
  //
  //    The two branches below MUST touch the exact same elements/properties
  //    (text + tspan, fill + color) and always call setProperty with an
  //    explicit priority. This component reuses the same rendered SVG DOM
  //    across theme toggles (re-running only this function, not a fresh
  //    mermaid.render), so if a diagram was ever painted once in dark mode
  //    with the "important" branch, whatever property the light-mode branch
  //    forgets to touch is left stuck at its last forced value. That used to
  //    leave `color: #ffffff !important` behind on light mode (fill got
  //    reset, color did not) — white text was then unreadable on light
  //    backgrounds after switching themes.
  svg.querySelectorAll('text, tspan').forEach((t) => {
    const el = t as SVGTextElement;
    const value = forceWhiteText ? '#ffffff' : textColor;
    const priority = forceWhiteText ? 'important' : '';
    el.style.setProperty('fill', value, priority);
    el.style.setProperty('color', value, priority);
    el.setAttribute('fill', value);
  });

  // 2. Shape fills -> the theme's box fill so labels are legible on them.
  //    Includes the sequence-diagram boxes (.actor participant boxes, .note,
  //    .labelBox loop headers, activation bars).
  svg
    .querySelectorAll(
      '.node rect, .node circle, .node ellipse, .node polygon, .node path, .cluster rect, ' +
        'rect.actor, .actor, .note, .labelBox, .activation0, .activation1, .activation2',
    )
    .forEach((el) => {
      // .actor matches both the participant <rect> and its <text> (text.actor);
      // never restyle text here — step 1 owns text color.
      if (el.tagName.toLowerCase() === 'text' || el.closest('text')) return;
      el.setAttribute('fill', boxFill);
      (el as SVGGraphicsElement).style.fill = boxFill;
      el.setAttribute('stroke', boxStroke);
      (el as SVGGraphicsElement).style.stroke = boxStroke;
    });

  // 3. HTML labels inside <foreignObject> (flowchart/class/state htmlLabels) ->
  //    theme text color, transparent background, on every descendant so nothing
  //    inherits a mismatched page foreground. For sequence diagrams in dark
  //    mode, force white with !important (same reason as step 1 — beat the
  //    injected fdwStyle that forces #1a1a1a !important).
  svg.querySelectorAll('foreignObject').forEach((fo) => {
    const value = forceWhiteText ? '#ffffff' : textColor;
    const priority = forceWhiteText ? 'important' : '';
    (fo as SVGElement).style.setProperty('color', value, priority);
    fo.querySelectorAll('*').forEach((child) => {
      const c = child as HTMLElement;
      c.style.setProperty('color', value, priority);
      c.style.backgroundColor = 'transparent';
    });
  });

  // 4. Connector lines and arrowheads: in dark mode a black stroke/marker would
  //    vanish on the dark canvas. Only well-known mermaid connector classes and
  //    <marker> shapes are touched — never node/box shapes and never text.
  if (isDark) {
    svg
      .querySelectorAll(
        '.edgePath .path, .flowchart-link, .messageLine0, .messageLine1, ' +
          '.loopLine, .actor-line, .relation, .transition',
      )
      .forEach((el) => {
        el.setAttribute('stroke', lineColor);
        (el as SVGGraphicsElement).style.stroke = lineColor;
      });
    svg.querySelectorAll('marker path, marker polygon').forEach((el) => {
      el.setAttribute('fill', lineColor);
      (el as SVGGraphicsElement).style.fill = lineColor;
      el.setAttribute('stroke', lineColor);
      (el as SVGGraphicsElement).style.stroke = lineColor;
    });
  }
}

interface MermaidProps {
  chart: string;
  className?: string;
  zoomingEnabled?: boolean;
}

// Full screen modal component for the diagram.
// Fills the whole viewport and drives the SVG with svg-pan-zoom, so large
// workflow diagrams can be freely panned (drag) and zoomed (wheel / buttons)
// instead of being squeezed into a small fixed-size box.
const FullScreenModal: React.FC<{
  isOpen: boolean;
  onClose: () => void;
  svg: string;
  labels: {
    title: string;
    zoomOut: string;
    zoomIn: string;
    resetZoom: string;
    close: string;
  };
}> = ({ isOpen, onClose, svg, labels }) => {
  const contentRef = useRef<HTMLDivElement>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const panZoomRef = useRef<any>(null);

  // Close on Escape key
  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  // Initialize pan/zoom on the rendered SVG each time the modal opens
  useEffect(() => {
    if (!isOpen) return;
    let disposed = false;

    const timer = setTimeout(async () => {
      const svgElement = contentRef.current?.querySelector('svg');
      if (!svgElement || disposed) return;
      // Force black-on-white labels in the fullscreen view too (scoped to the
      // modal's SVG container — same bulletproof inline-style pass).
      forceMermaidReadable(contentRef.current);
      svgElement.style.maxWidth = 'none';
      svgElement.style.width = '100%';
      svgElement.style.height = '100%';
      try {
        const svgPanZoom = (await import('svg-pan-zoom')).default;
        if (disposed) return;
        panZoomRef.current = svgPanZoom(svgElement, {
          zoomEnabled: true,
          panEnabled: true,
          controlIconsEnabled: false,
          fit: true,
          center: true,
          minZoom: 0.1,
          maxZoom: 20,
          zoomScaleSensitivity: 0.35,
        });
      } catch (error) {
        console.error('Failed to load svg-pan-zoom:', error);
      }
    }, 60);

    return () => {
      disposed = true;
      clearTimeout(timer);
      try {
        panZoomRef.current?.destroy();
      } catch {
        // instance already gone
      }
      panZoomRef.current = null;
    };
  }, [isOpen, svg]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[100] bg-black/85 p-2 sm:p-4">
      <div className="bg-[var(--card-bg)] rounded-lg shadow-custom w-full h-full overflow-hidden flex flex-col card-japanese">
        {/* Modal header with controls */}
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-[var(--border-color)] shrink-0">
          <div className="font-medium text-[var(--foreground)] font-mono text-sm">{labels.title}</div>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <button
                onClick={() => panZoomRef.current?.zoomOut()}
                className="text-[var(--foreground)] hover:bg-[var(--accent-primary)]/10 p-2 rounded-md border border-[var(--border-color)] transition-colors"
                aria-label={labels.zoomOut}
                title={labels.zoomOut}
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="11" cy="11" r="8"></circle>
                  <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                  <line x1="8" y1="11" x2="14" y2="11"></line>
                </svg>
              </button>
              <button
                onClick={() => panZoomRef.current?.zoomIn()}
                className="text-[var(--foreground)] hover:bg-[var(--accent-primary)]/10 p-2 rounded-md border border-[var(--border-color)] transition-colors"
                aria-label={labels.zoomIn}
                title={labels.zoomIn}
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="11" cy="11" r="8"></circle>
                  <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                  <line x1="11" y1="8" x2="11" y2="14"></line>
                  <line x1="8" y1="11" x2="14" y2="11"></line>
                </svg>
              </button>
              <button
                onClick={() => {
                  panZoomRef.current?.resetZoom();
                  panZoomRef.current?.center();
                  panZoomRef.current?.fit();
                }}
                className="text-[var(--foreground)] hover:bg-[var(--accent-primary)]/10 p-2 rounded-md border border-[var(--border-color)] transition-colors"
                aria-label={labels.resetZoom}
                title={labels.resetZoom}
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"></path>
                  <path d="M21 3v5h-5"></path>
                </svg>
              </button>
            </div>
            <button
              onClick={onClose}
              className="text-[var(--foreground)] hover:bg-[var(--accent-primary)]/10 p-2 rounded-md border border-[var(--border-color)] transition-colors"
              aria-label={labels.close}
              title={labels.close}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="18" y1="6" x2="6" y2="18"></line>
                <line x1="6" y1="6" x2="18" y2="18"></line>
              </svg>
            </button>
          </div>
        </div>

        {/* Diagram canvas — the SVG fills all remaining space; drag to pan, wheel to zoom */}
        <div
          ref={contentRef}
          className="flex-1 min-h-0 bg-[var(--background)]/50 cursor-grab active:cursor-grabbing [&>svg]:w-full [&>svg]:h-full"
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>
    </div>
  );
};

const Mermaid: React.FC<MermaidProps> = ({ chart, className = '', zoomingEnabled = false }) => {
  const { messages } = useLanguage();
  const { resolvedTheme } = useTheme();
  const labels = {
    title: messages.diagram?.title || 'Diagram view',
    renderError: messages.diagram?.renderError || 'Diagram rendering error',
    syntaxError: messages.diagram?.syntaxError || 'The diagram contains invalid Mermaid syntax and could not be rendered.',
    source: messages.diagram?.source || 'Diagram source',
    rendering: messages.diagram?.rendering || 'Rendering diagram...',
    clickToZoom: messages.diagram?.clickToZoom || 'Click to zoom',
    zoomOut: messages.diagram?.zoomOut || 'Zoom out',
    zoomIn: messages.diagram?.zoomIn || 'Zoom in',
    resetZoom: messages.diagram?.resetZoom || 'Reset zoom',
    close: messages.common?.close || 'Close',
  };
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const idRef = useRef(`mermaid-${Math.random().toString(36).substring(2, 9)}`);

  // Initialize pan-zoom functionality when SVG is rendered
  useEffect(() => {
    if (svg && zoomingEnabled && containerRef.current) {
      const initializePanZoom = async () => {
        const svgElement = containerRef.current?.querySelector("svg");
        if (svgElement) {
          // Remove any max-width constraints
          svgElement.style.maxWidth = "none";
          svgElement.style.width = "100%";
          svgElement.style.height = "100%";

          try {
            // Dynamically import svg-pan-zoom only when needed in the browser
            const svgPanZoom = (await import("svg-pan-zoom")).default;

            svgPanZoom(svgElement, {
              zoomEnabled: true,
              controlIconsEnabled: true,
              fit: true,
              center: true,
              minZoom: 0.1,
              maxZoom: 10,
              zoomScaleSensitivity: 0.3,
            });
          } catch (error) {
            console.error("Failed to load svg-pan-zoom:", error);
          }
        }
      };

      // Wait for the SVG to be rendered
      setTimeout(() => {
        void initializePanZoom();
      }, 100);
    }
  }, [svg, zoomingEnabled]);

  useEffect(() => {
    if (!chart) return;

    let isMounted = true;

    const renderChart = async () => {
      if (!isMounted) return;

      try {
        setError(null);
        setSvg('');

        const normalizedChart = normalizeMermaidChart(chart);
        await mermaid.parse(normalizedChart);
        const { svg: renderedSvg } = await mermaid.render(
          idRef.current,
          normalizedChart,
        );

        if (!isMounted) return;

        let processedSvg = renderedSvg;
        // Force readable labels regardless of app/OS theme.
        //
        // Mermaid renders node/edge labels as HTML inside <foreignObject>. Those
        // elements inherit `color` from the page, so on a dark-themed page
        // (next-themes sets <html class="dark">) the labels inherit a light
        // foreground and become invisible on the light cream node fills. Mermaid's
        // own themeCSS targets specific label classes, but that is unreliable
        // across diagram types and mermaid versions, and it depends on a
        // data-theme attribute we used to derive from the OS prefers-color-scheme
        // (which does not match the app theme). Instead, inject a high-specificity
        // <style> directly into the SVG that forces dark text on every
        // foreignObject descendant and a dark fill on every <text>, plus light
        // node fills — unconditionally. Diagrams are therefore always
        // black-on-cream and legible no matter the page theme.
        // The base rules below are theme-blind: they always force dark text on
        // a light node fill. That's fine in light mode, but in dark mode
        // forceMermaidReadable() repaints boxes dark via plain inline styles
        // (no !important), and those lose to this stylesheet's !important
        // rules for EVERY diagram type that renders labels as HTML inside
        // <foreignObject> (flowchart, classDiagram, stateDiagram, erDiagram,
        // …) — not just sequence diagrams. The result is near-black text on a
        // dark box, unreadable. Add a `[data-theme="dark"]`-scoped mirror of
        // every rule so the stylesheet itself matches the current theme
        // instead of fighting forceMermaidReadable's JS-set colors. Sequence
        // diagrams get pure white (matching forceMermaidReadable's
        // forceWhiteText) via a higher-priority override later in the sheet;
        // every other diagram type gets the same light-gray (#e8e8f2) text
        // forceMermaidReadable uses elsewhere.
        const isSequence = /^\s*sequenceDiagram\b/im.test(normalizedChart);
        const sequenceOverride = isSequence
          ? `
[data-theme="dark"] [data-fdw-sequence] text { fill: #ffffff !important; color: #ffffff !important; }
[data-theme="dark"] [data-fdw-sequence] foreignObject, [data-theme="dark"] [data-fdw-sequence] foreignObject * { color: #ffffff !important; }`
          : '';
        const fdwStyle = `<style>
[data-fdw-mermaid] foreignObject, [data-fdw-mermaid] foreignObject * { color: #1a1a1a !important; background-color: transparent !important; }
[data-fdw-mermaid] text { fill: #1a1a1a !important; color: #1a1a1a !important; }
[data-fdw-mermaid] .node rect, [data-fdw-mermaid] .node circle, [data-fdw-mermaid] .node ellipse, [data-fdw-mermaid] .node polygon, [data-fdw-mermaid] .cluster rect { fill: #f8f4e6 !important; stroke: #b9a89f !important; }
[data-fdw-mermaid] .edgePath .path { stroke: #7a5ca8 !important; stroke-width: 1.5px !important; }
[data-theme="dark"] [data-fdw-mermaid] foreignObject, [data-theme="dark"] [data-fdw-mermaid] foreignObject * { color: #e8e8f2 !important; background-color: transparent !important; }
[data-theme="dark"] [data-fdw-mermaid] text { fill: #e8e8f2 !important; color: #e8e8f2 !important; }
[data-theme="dark"] [data-fdw-mermaid] .node rect, [data-theme="dark"] [data-fdw-mermaid] .node circle, [data-theme="dark"] [data-fdw-mermaid] .node ellipse, [data-theme="dark"] [data-fdw-mermaid] .node polygon, [data-theme="dark"] [data-fdw-mermaid] .cluster rect { fill: #24283b !important; stroke: #565f89 !important; }
[data-theme="dark"] [data-fdw-mermaid] .edgePath .path { stroke: #a9b1d6 !important; stroke-width: 1.5px !important; }${sequenceOverride}
</style>`;
        processedSvg = processedSvg.replace('<svg ', `<svg data-fdw-mermaid="1"${isSequence ? ' data-fdw-sequence="1"' : ''} `);
        processedSvg = processedSvg.replace(/^(<svg[^>]*>)/, `$1${fdwStyle}`);

        setSvg(processedSvg);

        // Call mermaid.contentLoaded to ensure proper initialization
        setTimeout(() => {
          mermaid.contentLoaded();
        }, 50);
      } catch (err) {
        console.error('Mermaid rendering error:', err);

        const errorMessage = err instanceof Error ? err.message : String(err);

        if (isMounted) {
          setError(`Failed to render diagram: ${errorMessage}`);

        }
      }
    };

    renderChart();

    return () => {
      isMounted = false;
    };
  }, [chart]);

  // After the SVG string is committed to the DOM, force every label/shape to
  // the theme-matched palette (dark canvas + light text in dark mode, white
  // canvas + black text in light mode) via inline styles. Re-runs when the user
  // toggles the theme so already-rendered diagrams restyle in place. Scoped to
  // this component's container only — wiki prose (rendered elsewhere) is
  // untouched.
  useEffect(() => {
    if (!svg) return;
    const apply = () => forceMermaidReadable(containerRef.current);
    // DOM is updated synchronously after setSvg, but run on next tick too in
    // case dangerouslySetInnerHTML hasn't flushed yet.
    apply();
    const id = window.setTimeout(apply, 0);
    return () => window.clearTimeout(id);
  }, [svg, resolvedTheme]);

  const handleDiagramClick = () => {
    if (!error && svg) {
      setIsFullscreen(true);
    }
  };

  if (error) {
    return (
      <div className={`border border-[var(--highlight)]/30 rounded-md p-4 bg-[var(--highlight)]/5 ${className}`}>
        <div className="flex items-center mb-3">
          <div className="text-[var(--highlight)] text-xs font-medium flex items-center">
            <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 mr-2" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
            {labels.renderError}
          </div>
        </div>
        <details className="text-xs">
          <summary className="cursor-pointer text-[var(--muted)]">
            {labels.source}
          </summary>
          <pre className="mt-2 overflow-auto p-2 bg-[var(--background)]/60 border border-[var(--border-color)] rounded whitespace-pre-wrap">
            {chart}
          </pre>
        </details>
        <div className="mt-3 text-xs text-[var(--muted)] font-serif">
          {labels.syntaxError}
        </div>
      </div>
    );
  }

  if (!svg) {
    return (
      <div className={`flex justify-center items-center p-4 ${className}`}>
        <div className="flex items-center space-x-2">
          <div className="w-2 h-2 bg-[var(--accent-primary)]/70 rounded-full animate-pulse"></div>
          <div className="w-2 h-2 bg-[var(--accent-primary)]/70 rounded-full animate-pulse delay-75"></div>
          <div className="w-2 h-2 bg-[var(--accent-primary)]/70 rounded-full animate-pulse delay-150"></div>
          <span className="text-[var(--muted)] text-xs ml-2 font-serif">{labels.rendering}</span>
        </div>
      </div>
    );
  }

  return (
    <>
      <div
        ref={containerRef}
        className={`w-full max-w-full ${zoomingEnabled ? "h-[600px] p-4" : ""}`}
      >
        <div
          className={`relative group ${zoomingEnabled ? "h-full rounded-lg border-2 border-[var(--border-color)]" : ""}`}
        >
          <div
            className={`flex justify-center overflow-auto text-center my-2 cursor-pointer hover:shadow-md transition-shadow duration-200 rounded-md ${className} ${zoomingEnabled ? "h-full" : ""}`}
            dangerouslySetInnerHTML={{ __html: svg }}
            onClick={zoomingEnabled ? undefined : handleDiagramClick}
            title={zoomingEnabled ? undefined : labels.clickToZoom}
          />

          {!zoomingEnabled && (
            <div className="absolute top-2 right-2 bg-[var(--card-bg)] border border-[var(--border-color)] text-[var(--foreground)] p-1.5 rounded-md opacity-0 group-hover:opacity-100 transition-opacity duration-200 flex items-center gap-1.5 text-xs shadow-md pointer-events-none">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="11" cy="11" r="8"></circle>
                <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                <line x1="11" y1="8" x2="11" y2="14"></line>
                <line x1="8" y1="11" x2="14" y2="11"></line>
              </svg>
              <span>{labels.clickToZoom}</span>
            </div>
          )}
        </div>
      </div>

      {!zoomingEnabled && (
        <FullScreenModal
          isOpen={isFullscreen}
          onClose={() => setIsFullscreen(false)}
          svg={svg}
          labels={{
            title: labels.title,
            zoomOut: labels.zoomOut,
            zoomIn: labels.zoomIn,
            resetZoom: labels.resetZoom,
            close: labels.close,
          }}
        />
      )}
    </>
  );
};



export default Mermaid;
