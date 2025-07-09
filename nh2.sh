#!/bin/bash

# help info
if [ "$1" == "help" ] ; then
	echo This is a script used to download and nhentai as pictures
	echo
	echo "(./)nh2.sh [NUMBER|help]"
	echo
	echo NUMBER is the magic number of the gallery
	echo or you may as well use help to print out this help messege
	exit 0
fi


cd ~/nh || exit 1
if [ -e "$1".cbz ]; then
	echo "Already downloaded this gallery!"
	exit 0
fi
mkdir "$1" || echo "Folder already exists"
cd "$1" || exit 1

# directory for saving the image
cd ~/nh || exit 1
if [ -e "$1".cbz ]; then
	echo "Already downloaded this gallery!"
	exit 0
fi
mkdir "$1" || echo "Folder already exists"
cd "$1" || exit 1

# fetch the cover page and save it
declare COVER_HTML="$(wget -q -O - https://nhentai.net/g/"$1"/)"
echo "$COVER_HTML" > cover_page.html

# a command to download with auto-retrying
download-with-auto-retry() {
	declare FILENAME=$1
	declare URL=$2
	touch "$FILENAME"

	wget -q -O "$FILENAME" "$URL"
	declare LAST_WGET_DOWNLOAD_RET=$?
	declare MAX_RETRY=5
	declare MEDIA_SERVER_LIST=(3 7 5)
		# by using `dig i${n}.nhentai.net`, we can see only these three
		# servers have IPv4 addresses and are thus valid

	for i in $(seq 1 "$MAX_RETRY"); do 
		# retry if wget didn't run successfully
		if [ "$LAST_WGET_DOWNLOAD_RET" -eq 0 ]; then
			break;
		fi 
		declare ALTER_MEDIA_SERVER_IDX=$(((i - 1) % ${#MEDIA_SERVER_LIST[@]}))
		declare ALTER_MEDIA_SERVER=${MEDIA_SERVER_LIST[ALTER_MEDIA_SERVER_IDX]}
		declare ALTER_URL=$(echo "$URL" | sed -E "s/\/\/(i|t)[0-9]*\./\/\/\1${ALTER_MEDIA_SERVER}./")
		echo "$FILENAME error. Retrying with media_server=$ALTER_MEDIA_SERVER ($i/$MAX_RETRY)..."
		wget -q -O "$FILENAME" "$ALTER_URL"
		LAST_WGET_DOWNLOAD_RET=$?
	done

curl -s https://nhentai.net/g/"$1"/ > tmp.html

if [ "$(grep -o -E "404 - Not Found" tmp.html)" == "404 - Not Found" ]; then
	echo "Gallery does not exists"
	cd ..
	rm -r "$1"
	exit 1
fi

for i in $(seq 1 500)
do
#skip if image already exists, should be alright
	if [ -e "$i".jpg ]; then
		continue
	fi
	if [ -e "$i".png ]; then
		continue
	fi
	if [ -e "$i".gif ]; then
		continue
	fi
}

# extract a list of images that we need to download
# make enter after each html tag 
# -> grep the urls 
#        - pattern: cover.jpg/png/gif, and t${number}.jpg/png/gif for thumbnails
# -> convert thumbnail filenames to normal files
# -> uniquify the links with awk
#    - notice: there may still be multiple urls for book covers
declare IMAGE_URLS="$(echo "$COVER_HTML" \
	| sed -E 's/>/\n/g' \
	| grep -oEe 'https://t[0-9]+.nhentai\.net/galleries/[0-9]+/[0-9]+t\.[a-zA-Z]+' \
	| sed -E 's/t(\.[a-zA-Z]{1,10})$/\1/g' | sed -E 's/\/\/t([0-9]+)\./\/\/i\1./' \
	| awk '!a[$0]++'
)"

# download each image
declare JOBS=()
declare MAX_JOB_COUNT=20

update-jobs() {
	declare UPDATED=()
	for PID in "${JOBS[@]}"; do 
		if ps -p "$PID" >> /dev/null ; then
			UPDATED+=("$PID")
		fi
	done;
	JOBS=("${UPDATED[@]}")
}

#download html of nhentai.net/g/NUMBER/$i
	curl -s https://nhentai.net/g/"$1"/"$i"/ > tmp.html

#check if it's 404 or not, break if it is
	# echo $(grep -o -e "404 - Not Found" tmp.html)
	if [ "$(grep -o -e "404 - Not Found" tmp.html)" == "404 - Not Found" ]; then
		max_page=$((i-1))
		break
	fi


#wget to download it
	file_type=${img#*.}
	file_type=${file_type#*.}
	file_type=${file_type#*.}
	if [ ! -f "$i"."$file_type" ]; then
		wget -q "$img" &
	fi
	echo "$1: $i"
done

#echo $max_page

# recheck
for k in $(seq 1 50)
do
	flag=0
	sleep 1 #wait for images to be downloaded
	for j in $(seq 1 10)
	do
		flag=0
		for i in $(seq 1 $max_page)
		do
			#skip if file already exists, ought to be right
			if [ -e "$i".jpg ]; then
				continue
			fi
			if [ -e "$i".png ]; then
				continue
			fi
			if [ -e "$i".gif ]; then
				continue
			fi

			#flaged since there's still image loss
			flag=1

			#resend html request
			curl -s https://nhentai.net/g/"$1"/"$i"/ > tmp.html
			img=$(grep -o -E "https://i[1-9].nhentai.net/galleries/[0-9]*/[1-9][0-9]*.(jpg|png|gif)" tmp.html)

			wget -q "$img" &
			# echo "$i "
		done

		if [ $flag -eq 0 ]; then
			break
		fi
	done
	if [ $flag -eq 0 ]; then
		break
	fi
done

rm tmp.html
# rm tmp*.html
# ls | grep -P "[0|1|2|3|4|5|6|7|8|9]$" | xargs -d"\n" rm
