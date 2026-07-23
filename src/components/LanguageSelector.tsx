'use client';

import React from 'react';
import { useLanguage } from '@/contexts/LanguageContext';

export default function LanguageSelector() {
  const { language, setLanguage, supportedLanguages } = useLanguage();

  return (
    <select
      value={language}
      onChange={(e) => setLanguage(e.target.value)}
      className="bg-transparent border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--foreground)] text-sm rounded-md px-2 py-1 outline-none focus:border-[var(--accent-primary)] transition-colors cursor-pointer"
      aria-label="Select Language"
    >
      {Object.entries(supportedLanguages).map(([code, name]) => (
        <option key={code} value={code} className="bg-[var(--card-bg)] text-[var(--foreground)]">
          {name}
        </option>
      ))}
    </select>
  );
}
