const FLOWCHART_HEADER = /^\s*(?:graph|flowchart)\s+(?:TB|TD|BT|RL|LR)\b/m;

// Mermaid's flowchart lexer breaks on unquoted (), [], {} or " inside a
// pipe-delimited edge label (e.g. `-->|Respuesta (stream)|`), even though the
// same punctuation is fine inside a quoted node label. LLMs routinely emit
// these unquoted, so any label containing one of these characters needs
// wrapping in quotes.
const NEEDS_QUOTING = /[()[\]{}"]/;

/**
 * Mermaid flowcharts require quoted labels when generated text contains
 * punctuation such as parentheses. LLMs frequently omit those quotes.
 * Normalize square-node labels and pipe-delimited edge labels; other diagram
 * types and shapes remain untouched.
 */
export const normalizeMermaidChart = (chart: string): string => {
  const normalizedLines = chart.trim().replace(/\r\n/g, '\n');
  if (!FLOWCHART_HEADER.test(normalizedLines)) {
    return normalizedLines;
  }

  const withQuotedNodeLabels = normalizedLines.replace(
    /(\b[A-Za-z_][A-Za-z0-9_-]*)\[([^\]\n]+)\]/g,
    (match, nodeId: string, rawLabel: string) => {
      const label = rawLabel.trim();
      if (
        (label.startsWith('"') && label.endsWith('"')) ||
        (label.startsWith("'") && label.endsWith("'"))
      ) {
        return match;
      }
      const escapedLabel = label.replace(/"/g, '&quot;');
      return `${nodeId}["${escapedLabel}"]`;
    },
  );

  return withQuotedNodeLabels.replace(
    /\|([^|\n]+)\|/g,
    (match, rawLabel: string) => {
      const label = rawLabel.trim();
      if (
        (label.startsWith('"') && label.endsWith('"')) ||
        (label.startsWith("'") && label.endsWith("'")) ||
        !NEEDS_QUOTING.test(label)
      ) {
        return match;
      }
      const escapedLabel = label.replace(/"/g, '&quot;');
      return `|"${escapedLabel}"|`;
    },
  );
};
