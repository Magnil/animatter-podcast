/**
 * Browser-side delivery of a downloaded file: one `<a download>` click.
 *
 * Extracted from `App.tsx` when the video panel learned to download a `.template` package
 * (round 138). Two copies of this would drift on the only line that matters — the 60 s delay
 * before `revokeObjectURL`, which exists because revoking immediately cancels the download in
 * some browsers.
 *
 * Not unit-tested: it is three DOM calls with no branch worth pinning. What *is* tested is
 * the filename arithmetic next door in `templatePackage.ts`.
 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoking immediately can cancel the download in some browsers; one minute is generous
  // and the blob is small relative to the audio it describes.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
