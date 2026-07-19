export const DEFAULT_COMPREHENSIVE_PAGE_COUNT = 10;
export const DEFAULT_CONCISE_PAGE_COUNT = 5;
export const MIN_WIKI_PAGE_COUNT = 1;
export const MAX_WIKI_PAGE_COUNT = 50;

export const getDefaultWikiPageCount = (isComprehensive: boolean): number =>
  isComprehensive
    ? DEFAULT_COMPREHENSIVE_PAGE_COUNT
    : DEFAULT_CONCISE_PAGE_COUNT;

export const normalizeWikiPageCount = (
  value: string | number | null | undefined,
  isComprehensive: boolean,
): number => {
  const parsed = typeof value === 'number' ? value : Number.parseInt(value || '', 10);
  if (!Number.isFinite(parsed)) {
    return getDefaultWikiPageCount(isComprehensive);
  }
  return Math.min(
    MAX_WIKI_PAGE_COUNT,
    Math.max(MIN_WIKI_PAGE_COUNT, Math.round(parsed)),
  );
};
