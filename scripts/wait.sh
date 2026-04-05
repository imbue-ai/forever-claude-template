#!/usr/bin/env bash
# Idle backoff wait script.
#
# Reads a counter from .runtime/wait_counter, looks up a sleep duration
# from a schedule, sleeps, then increments the counter.
#
# The counter file is deleted by the UserPromptSubmit and Notification[idle_prompt]
# Claude hooks when a real message arrives, resetting the backoff.
#
# Schedule: minutes to sleep for each consecutive idle cycle.
# The last value repeats forever.
SCHEDULE=(1 1 5 10 30 60)

COUNTER_FILE=".runtime/wait_counter"

mkdir -p .runtime

# Read current counter (default 0)
if [ -f "$COUNTER_FILE" ]; then
    counter=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
    # Validate it's a number
    if ! [[ "$counter" =~ ^[0-9]+$ ]]; then
        counter=0
    fi
else
    counter=0
fi

# Look up sleep duration from schedule
schedule_len=${#SCHEDULE[@]}
if [ "$counter" -ge "$schedule_len" ]; then
    # Past the end of the schedule: use the last value
    sleep_minutes=${SCHEDULE[$((schedule_len - 1))]}
else
    sleep_minutes=${SCHEDULE[$counter]}
fi

sleep_seconds=$((sleep_minutes * 60))

echo "Idle cycle $counter: sleeping for $sleep_minutes minute(s)..."

# Increment counter and write back
echo $((counter + 1)) > "$COUNTER_FILE"

# Sleep
sleep "$sleep_seconds"

echo "Wake up after $sleep_minutes minute(s) of idle sleep."
