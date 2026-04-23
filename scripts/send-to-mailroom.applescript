-- Send selected Outlook email(s) to Mailroom.
-- Exports each selected message as an .eml file into ~/mailroom/inbox/ where the
-- watcher picks it up within ~2 seconds and the LLM parser extracts the fields.
--
-- Bound via Shortcuts.app "Run AppleScript" action with hotkey ⌃⌥⌘M.
--
-- Design notes:
--   - All AppleScript that TALKS TO Outlook is wrapped in tell/end tell.
--   - User-facing dialogs and notifications are OUTSIDE that block — otherwise
--     constants like `note` and `caution` get resolved against Outlook's
--     dictionary (where `note` is an object class) and error with "Can't make
--     note into type number or string".
--   - Falls back to reading raw MIME via `source` if the native `save` path
--     fails on a given Outlook build.

on run
    set inboxPath to (POSIX path of (path to home folder)) & "mailroom/inbox/"
    do shell script "mkdir -p " & quoted form of inboxPath

    -- Grab the selection, then exit the tell block ASAP.
    set theSelection to my outlookSelection()

    if (count of theSelection) is 0 then
        set diag to my outlookSelectionDiagnostic()
        display dialog "No email selection detected." & return & return & ¬
            "Outlook selection accessors reported:" & return & diag & return & ¬
            "If every line says 0 items, click the email in the message list (not the reading pane) and try again." ¬
            buttons {"OK"} default button "OK" with title "Mailroom"
        return
    end if

    set successCount to 0
    set failures to {}

    repeat with theMessage in theSelection
        set theSubject to my messageSubject(theMessage)

        set safeSubject to do shell script ¬
            "printf '%s' " & quoted form of (theSubject as string) & ¬
            " | tr -c '[:alnum:] ._\\-()#&' '_' | cut -c1-80"
        set timeStamp to do shell script "date +%Y%m%d-%H%M%S"
        set filePath to inboxPath & timeStamp & "-" & safeSubject & ".eml"

        -- Primary: Outlook's native save command.
        set saved to my saveMessage(theMessage, filePath)

        -- Fallback: grab raw MIME source and write the file ourselves.
        if not saved then
            try
                set mimeSource to my messageSource(theMessage)
                set fileRef to open for access (POSIX file filePath) with write permission
                set eof fileRef to 0
                write mimeSource to fileRef
                close access fileRef
                set saved to true
            on error errMsg
                try
                    close access (POSIX file filePath)
                end try
                copy ("" & theSubject & ": " & errMsg) to end of failures
            end try
        end if

        if saved then set successCount to successCount + 1
    end repeat

    set plural to "s"
    if successCount is 1 then set plural to ""

    if successCount > 0 then
        display notification ((successCount as string) & " email" & plural & ¬
            " sent to Mailroom inbox") with title "Mailroom"
    end if

    if (count of failures) > 0 then
        set failMsg to "Could not export " & (count of failures as string) & ¬
            " message(s):" & return & return
        repeat with f in failures
            set failMsg to failMsg & "• " & (f as string) & return
        end repeat
        display dialog failMsg buttons {"OK"} default button "OK" with title "Mailroom"
    end if
end run


-- New Outlook for Mac (16.79+) has a slimmer AppleScript dictionary than Classic,
-- and the "right" selection accessor depends on which pane has focus. Try each
-- known variant and return the first non-empty result. If all come back empty,
-- we hand back an empty list plus a diagnostic string via the probe handler.
on outlookSelection()
    tell application "Microsoft Outlook"
        activate
    end tell
    delay 0.25 -- let Outlook settle after activation
    try
        tell application "Microsoft Outlook" to set s to selected objects
        if s is not missing value and (count of s) > 0 then return s
    end try
    try
        tell application "Microsoft Outlook" to set s to current messages
        if s is not missing value and (count of s) > 0 then return s
    end try
    try
        tell application "Microsoft Outlook" to set s to selection
        if s is not missing value and (count of s) > 0 then return s
    end try
    try
        tell application "Microsoft Outlook" to set s to selection of front window
        if s is not missing value and (count of s) > 0 then return s
    end try
    return {}
end outlookSelection

-- Returns a short human-readable diagnostic of what each accessor produced.
-- Shown in the "no selection" dialog so you can see which accessors Outlook
-- actually supports on this build.
on outlookSelectionDiagnostic()
    set report to {}
    try
        tell application "Microsoft Outlook" to set s to selected objects
        if s is missing value then
            copy "selected objects: missing value" to end of report
        else
            copy ("selected objects: " & (count of s) & " items") to end of report
        end if
    on error e
        copy ("selected objects: error — " & e) to end of report
    end try
    try
        tell application "Microsoft Outlook" to set s to current messages
        if s is missing value then
            copy "current messages: missing value" to end of report
        else
            copy ("current messages: " & (count of s) & " items") to end of report
        end if
    on error e
        copy ("current messages: error — " & e) to end of report
    end try
    try
        tell application "Microsoft Outlook" to set s to selection
        if s is missing value then
            copy "selection: missing value" to end of report
        else
            copy ("selection: " & (count of s) & " items") to end of report
        end if
    on error e
        copy ("selection: error — " & e) to end of report
    end try
    set out to ""
    repeat with entry in report
        set out to out & (entry as string) & return
    end repeat
    return out
end outlookSelectionDiagnostic


on messageSubject(theMessage)
    tell application "Microsoft Outlook"
        try
            return subject of theMessage
        on error
            return "no-subject"
        end try
    end tell
end messageSubject


on saveMessage(theMessage, filePath)
    tell application "Microsoft Outlook"
        try
            save theMessage in POSIX file filePath
            return true
        on error
            return false
        end try
    end tell
end saveMessage


on messageSource(theMessage)
    tell application "Microsoft Outlook"
        return source of theMessage
    end tell
end messageSource
