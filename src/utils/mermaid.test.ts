import { describe, it, expect } from 'vitest';
import { normalizeMermaidChart } from './mermaid';

// normalizeMermaidChart is the *parser-level* fix that lets mermaid's lexer
// accept LLM-generated flowcharts whose labels contain (), [], {}, " unquoted.
// It is NOT the security sanitizer (that's securityLevel: 'strict' in
// Mermaid.tsx) -- but it must not introduce an injection hole of its own: any
// `"` inside a label is HTML-escaped to &quot; before being wrapped in
// quotes, so a label can't break out of its quoted attribute. These tests pin
// both the syntax fix and the escape behavior.

describe('normalizeMermaidChart', () => {
  it('is a no-op for non-flowchart diagrams', () => {
    const seq = `sequenceDiagram\nAlice->>Bob: Hi (there)`;
    expect(normalizeMermaidChart(seq)).toBe(seq);
  });

  it('quotes a square-node label containing parentheses', () => {
    const out = normalizeMermaidChart('flowchart TD\nA[Respuesta (stream)] --> B');
    expect(out).toContain('A["Respuesta (stream)"]');
  });

  it('does not re-quote an already-quoted label', () => {
    const src = 'flowchart TD\nA["already quoted (ok)"] --> B';
    expect(normalizeMermaidChart(src)).toBe(src);
  });

  it('does not re-quote a single-quoted label', () => {
    const src = "flowchart TD\nA['single (ok)'] --> B";
    expect(normalizeMermaidChart(src)).toBe(src);
  });

  it('escapes double-quotes inside a label before wrapping (no attribute breakout)', () => {
    // A label with a literal " must not be able to close the quoted attribute
    // and inject mermaid syntax. The inner " is escaped to &quot;.
    const out = normalizeMermaidChart('flowchart TD\nA[say "hi" (now)] --> B');
    expect(out).toContain('A["say &quot;hi&quot; (now)"]');
    // And crucially the label value never contains an unescaped " that could
    // close the attribute we just opened.
    const labelAttr = out.match(/A\["([^"]*)"\]/);
    expect(labelAttr).not.toBeNull();
    expect(labelAttr![1]).not.toContain('"');
  });

  it('quotes a pipe-delimited edge label containing punctuation', () => {
    const out = normalizeMermaidChart('flowchart TD\nA -->|Respuesta (stream)| B');
    expect(out).toContain('|"Respuesta (stream)"|');
  });

  it('leaves a pipe label with no special punctuation untouched', () => {
    const src = 'flowchart TD\nA -->|plain label| B';
    expect(normalizeMermaidChart(src)).toBe(src);
  });

  it('normalizes CRLF line endings before testing the header', () => {
    const crlf = 'flowchart TD\r\nA[Resp (x)]\r\n';
    const out = normalizeMermaidChart(crlf);
    expect(out).not.toContain('\r');
    expect(out).toContain('A["Resp (x)"]');
  });

  it('handles flowchart with LR direction too', () => {
    const out = normalizeMermaidChart('flowchart LR\nA[node (x)] --> B[plain]');
    expect(out).toContain('A["node (x)"]');
    // Every square-node label is quoted defensively (the function quotes all
    // node labels, not just those with punctuation); "plain" has no special
    // chars so it stays a clean quoted label, not mangled.
    expect(out).toContain('B["plain"]');
  });

  it('preserves the graph keyword header (alias of flowchart)', () => {
    const out = normalizeMermaidChart('graph TD\nA[label (with) parens]');
    expect(out).toContain('A["label (with) parens"]');
  });
});