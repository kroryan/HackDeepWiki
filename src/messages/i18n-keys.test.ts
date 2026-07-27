import { readdirSync, readFileSync } from 'fs';
import { join } from 'path';
import { describe, expect, it } from 'vitest';

// Every locale must carry every key en.json has. The UI falls back to the raw
// key (or English) when a translation is missing, which is exactly how the
// locales silently drifted 14-44 keys behind before this test existed.

const messagesDir = __dirname;

function flatten(obj: Record<string, unknown>, prefix = ''): string[] {
  return Object.entries(obj).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof value === 'object' && value !== null
      ? flatten(value as Record<string, unknown>, path)
      : [path];
  });
}

const enKeys = flatten(
  JSON.parse(readFileSync(join(messagesDir, 'en.json'), 'utf-8')),
);

const locales = readdirSync(messagesDir)
  .filter((name) => name.endsWith('.json') && name !== 'en.json');

describe('i18n message catalogs', () => {
  it('has at least one non-English locale', () => {
    expect(locales.length).toBeGreaterThan(0);
  });

  it.each(locales)('%s contains every key from en.json', (locale) => {
    const keys = new Set(
      flatten(JSON.parse(readFileSync(join(messagesDir, locale), 'utf-8'))),
    );
    const missing = enKeys.filter((key) => !keys.has(key));
    expect(missing).toEqual([]);
  });

  it.each(locales)('%s has no keys absent from en.json', (locale) => {
    const localeKeys = flatten(
      JSON.parse(readFileSync(join(messagesDir, locale), 'utf-8')),
    );
    const enSet = new Set(enKeys);
    const orphaned = localeKeys.filter((key) => !enSet.has(key));
    expect(orphaned).toEqual([]);
  });
});
