#!/bin/bash
while true; do
  avail=$(df -k /tmp | awk 'NR==2{print $4}')
  if [ "$avail" -gt 500000 ]; then
    echo "freed ${avail}K"
    break
  fi
  sleep 5
done
