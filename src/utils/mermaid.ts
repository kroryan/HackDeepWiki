const FLOWCHART_HEADER = /^\s*(?:graph|flowchart)\s+(?:TB|TD|BT|RL|LR)\b/m;

/**
 * Mermaid flowcharts require quoted labels when generated text contains
 * punctuation such as parentheses. LLMs frequently omit those quotes.
 * Normalize only square-node labels; other diagram types and shapes remain
 * untouched.
 */
export const normalizeMermaidChart = (chart: string): string => {
  const normalizedLines = chart.trim().replace(/\r\n/g, '\n');
  if (!FLOWCHART_HEADER.test(normalizedLines)) {
    return normalizedLines;
  }

  return normalizedLines.replace(
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
};
