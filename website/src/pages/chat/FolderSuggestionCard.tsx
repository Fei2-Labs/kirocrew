import { CornerDownRight } from 'lucide-react'
import { motion } from 'framer-motion'
import { Trans } from 'react-i18next'

import FolderGlyph from '../../components/FolderGlyph'
import { i18nT } from '../../i18n/t'

export interface FolderSuggestionCardProps {
  /** Folder name, and its root→leaf breadcrumb when the folder is nested. */
  folderName: string
  breadcrumb: string
  /** Palette color of the suggested folder, for the glyph tint. Omitted when the
   *  folder list has not loaded yet — the glyph then paints untinted rather than
   *  holding the card back. */
  folderColor?: string
  /** File the session into the suggested folder. */
  onAccept: () => void
  /** Leave the session where it is. The card is not re-offered either way. */
  onDecline: () => void
}

/**
 * "File this session in <folder>?" — offered once, after the session is titled,
 * for a session that is not in a folder yet.
 *
 * Rendered in the composer's own width box (via ChatInput's `aboveComposer`), so
 * it shares the tip's exact geometry. It takes precedence over the ambient tip
 * rather than stacking with it: the tip yields through `tipSuppressed` in
 * ChatPage, because two cards in that band is the crowding the band's priority
 * contract exists to prevent.
 *
 * Both buttons are terminal — there is nothing server-side to resolve, and the
 * backend offers at most one card per slot for that slot's lifetime, so
 * declining cannot be re-asked and accepting is a plain folder move the user can
 * undo from the sidebar.
 *
 * Answering is not the only way out: an untouched card ages out after
 * FOLDER_SUGGESTION_MAX_TURNS of the user's own confirmed sends made while this
 * card was on screen (chatSlice's `ageFolderSuggestion`, dispatched by the
 * ChatPage render site), so a wrong guess costs the composer band a few turns rather
 * than the whole session.
 */
export default function FolderSuggestionCard({ folderName, breadcrumb, folderColor, onAccept, onDecline }: FolderSuggestionCardProps) {
  // Show the breadcrumb only when it adds ancestry: for a root folder it is just
  // the name again, and rendering both reads as a duplicate.
  const parentPath = breadcrumb && breadcrumb !== folderName ? breadcrumb : ''

  return (
    <motion.div
      className="w-full flex items-center gap-2.5 px-4 py-2 rounded-md text-xs shadow-lg"
      style={{
        background: 'color-mix(in srgb, var(--accent) 6%, var(--bg-elevated))',
        border: '1px solid color-mix(in srgb, var(--accent) 12%, transparent)',
      }}
      initial={{ y: 6, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      exit={{ y: 4, opacity: 0 }}
      transition={{ duration: 0.25, ease: [0.2, 0.8, 0.2, 1] }}
      role="complementary"
      aria-label={i18nT('components.folderSuggestionCard.folder_suggestion')}
      data-testid="folder-suggestion-card"
    >
      {/* CornerDownRight, not a second folder: the destination now carries its
          own FolderGlyph inside the sentence, and two folder glyphs 14px apart
          read as two different folders. Mirrors SessionMoveUndoBar, which pairs
          the same rail arrow with the same destination glyph, and takes its
          `text-accent` token class rather than an inline accent. Stays at 14px:
          this card's lead icon was 14px before this PR, and shrinking it to the
          bar's 12px would resize a surface the diff is not otherwise touching.
          Always a lucide glyph, never the folder's own emoji: an emoji is a
          font-dependent bitmap that renders as a tofu box wherever the platform
          has no emoji font, and it would not inherit the accent. */}
      <CornerDownRight size={14} className="shrink-0 text-accent" aria-hidden="true" />

      <div className="min-w-0 flex-1">
        {/* Two copies of one question, and only one is ever perceived.
            FolderGlyph is aria-hidden, so the icon that replaced the WORD
            "folder" says nothing to a screen reader — on its own, AT would hear
            "Move this session to later?", the exact ambiguity this card's copy
            exists to remove. So the spoken key keeps the word and the quotes,
            and the visible line is aria-hidden to stop the question being
            announced twice. */}
        <span className="sr-only">
          {i18nT('components.folderSuggestionCard.move_to_folder_question_spoken', { folder: folderName })}
        </span>
        {/* One interpolated string with a self-closing <folder/> slot (the
            repo's Trans convention, cf. components.agentDropdownList
            .set_default_agent), NOT a prefix + name concatenation: the folder
            name never enters the catalog, and each locale places the slot where
            its own grammar wants it — ja/ko put the predicate last.
            A flex row rather than `truncate` on the whole line, because Trans
            emits the catalog's text as anonymous flex items around the slot:
            that keeps the locale's word order AND lets the name itself be the
            part that ellipsizes. `title` carries the full sentence, since for a
            ROOT folder the breadcrumb below is suppressed and this line is the
            only place the name appears. */}
        <span
          aria-hidden="true"
          data-testid="folder-suggestion-question"
          className="flex min-w-0 items-baseline whitespace-pre text-[12px] leading-tight"
          style={{ color: 'var(--text)' }}
          title={i18nT('components.folderSuggestionCard.move_to_folder_question_spoken', { folder: folderName })}
        >
          <Trans
            i18nKey="components.folderSuggestionCard.move_to_folder_question"
            components={{
              folder: (
                <span className="inline-flex min-w-0 items-baseline gap-1">
                  <FolderGlyph color={folderColor} size={12} className="shrink-0 translate-y-[2px]" />
                  <span data-testid="folder-suggestion-folder-name" className="truncate font-medium" style={{ color: 'var(--text-strong)' }}>{folderName}</span>
                </span>
              ),
            }}
          />
        </span>
        {parentPath && (
          <span className="block text-[11px] leading-tight mt-0.5 truncate" style={{ color: 'var(--muted)' }} title={parentPath}>
            {parentPath}
          </span>
        )}
      </div>

      <div className="flex items-center gap-1.5 shrink-0">
        <button
          onClick={onAccept}
          data-testid="folder-suggestion-accept"
          className="px-2.5 py-1 rounded text-[11px] font-medium hover:brightness-110 transition"
          style={{
            color: 'var(--accent)',
            background: 'color-mix(in srgb, var(--accent) 12%, transparent)',
            border: '1px solid color-mix(in srgb, var(--accent) 30%, transparent)',
          }}
        >
          {i18nT('components.folderSuggestionCard.yes_move_it')}
        </button>
        <button
          onClick={onDecline}
          data-testid="folder-suggestion-decline"
          className="px-2.5 py-1 rounded text-[11px] transition-colors hover:bg-[var(--bg-hover)]"
          style={{ color: 'var(--muted)', border: '1px solid var(--border)' }}
        >
          {i18nT('components.folderSuggestionCard.no_thanks')}
        </button>
      </div>
    </motion.div>
  )
}
