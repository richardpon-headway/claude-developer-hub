import { useQueryClient } from "@tanstack/react-query";

import { updateBookmarkNotes } from "../api/bookmarks";
import { NotesEditor } from "./NotesEditor";

interface Props {
  prRepo: string;
  prNumber: number;
  notes: string | null;
}

export function BookmarkNotes({ prRepo, prNumber, notes }: Props) {
  const queryClient = useQueryClient();
  return (
    <NotesEditor
      notes={notes}
      saveFn={(text) => updateBookmarkNotes(prRepo, prNumber, text)}
      onSaved={() => {
        queryClient.invalidateQueries({ queryKey: ["bookmarks"] });
      }}
    />
  );
}
