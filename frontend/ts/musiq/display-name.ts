/** Keep the original title when YouTube has already included the artist. */
export function displayArtist(artist, title): string {
  const label = String(artist || '').replace(/\s*[-–—]\s*Topic$|VEVO$/i, '');
  const words = value => String(value || '').normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
  const name = words(label);
  return name && (' ' + words(title) + ' ').includes(' ' + name + ' ') ? '' : String(artist || '');
}

export function displayName(song): string {
  return [displayArtist(song.artist, song.title), song.title].filter(Boolean).join(' – ');
}
