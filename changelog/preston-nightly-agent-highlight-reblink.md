The Caretaker's tab now blinks again on every run, and the blink is now a reusable building block:

- Previously, if you'd left the Caretaker's tab open, it would not blink again on later runs -- only a closed tab re-surfaced. Now the tab re-blinks for each new run whether it was closed (re-opened) or just sitting open in the background, so you always notice when the Caretaker has run again. (A tab you're actively looking at is left alone -- no point blinking what's already in front of you.)

- The blink is now driven by a generic `highlight` label rather than the Caretaker-specific `auto_created` label. Any agent can opt its tab into the blink by carrying a `highlight` label; the label's value is a key that, when bumped, makes the tab blink again. This generalizes the behavior beyond the Caretaker.
