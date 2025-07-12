#!/bin/bash

galleries=$(cat "$1" | wc -l)

i=1

while read -r line
do
	time ./nh2_requireCfToken.sh "$line" --max-retry=20 --media-server-list="1 2 3 4 5 6 7 8 9"
	echo "$i/$galleries done"
	i=$((i+1))
done < "$1"
