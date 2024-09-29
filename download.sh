#!/bin/bash

input="~/list.txt"
while read -r line
do
	./nh2.sh "$line"
done < "$input"
