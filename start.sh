#!/bin/bash
set -e

echo "Starting Track A (port $PORT)..."
uvicorn track_a.main:app --host 0.0.0.0 --port "$PORT" &
TRACK_A_PID=$!

echo "Starting Track B (port 8200)..."
uvicorn track_b.main:app --host 0.0.0.0 --port 8200 &
TRACK_B_PID=$!

# Trap SIGTERM to gracefully shut down both processes
cleanup() {
    echo "Shutting down..."
    kill $TRACK_A_PID $TRACK_B_PID 2>/dev/null
    wait $TRACK_A_PID $TRACK_B_PID 2>/dev/null
}
trap cleanup SIGTERM SIGINT

echo "Track A PID: $TRACK_A_PID, Track B PID: $TRACK_B_PID"
echo "Both services started."

# Wait for all background processes
wait
