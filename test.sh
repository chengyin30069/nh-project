#!/usr/bin/bash

# kill the whole process group of this script on Ctrl + C
# ref: https://stackoverflow.com/a/2173421
trap "trap - SIGTERM && kill -- -$$" SIGINT SIGTERM

# IDS="529160"
IDS="529160 528484 520419 514200 501156 501062 498434 498070 495835 493857 491474 486140 479564 464382 456202 447645 433951 429934 424562 422683 414786 400733 391273 391040"

if [ ! -e ~/nh ]; then 
	mkdir ~/nh
fi

echo "nh2.sh started at $(date +%Y%m%d-%H%M%S.%N)" > testlog
for i in $IDS; do 
	./nh2.sh "$i"
	rm ~/nh/"$i"/cover_page.html
done
echo "nh2.sh ended at $(date +%Y%m%d-%H%M%S.%N)" >> testlog

mv ~/nh ~/nh_new
mkdir ~/nh

echo "nh2_old.sh started at $(date +%Y%m%d-%H%M%S.%N)" >> testlog
for i in $IDS; do 
	./nh2_old.sh "$i"
done
echo "nh2_old.sh ended at $(date +%Y%m%d-%H%M%S.%N)" >> testlog

find ~/nh -type f -name "*.[1|2|3|4|5|6|7|8|9]*" -delete

diff -r ~/nh ~/nh_new >> testlog

echo 
echo 
echo "Here is the testlog: "
cat testlog
