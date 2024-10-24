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

# kill the whole process group of this script on Ctrl + C
# ref: https://stackoverflow.com/a/2173421
trap "trap - SIGTERM && kill -- -$$" SIGINT SIGTERM

# directory for saving the image
cd ~/nh
if [ -e "$1".cbz ]; then
	echo "Already downloaded"
	exit 0
fi
mkdir $1
cd $1

# fetch the cover page and save it
declare COVER_HTML="$(wget -q -O - https://nhentai.net/g/$1/)"
echo "$COVER_HTML" > cover_page.html

# a command to download with auto-retrying
download-with-auto-retry() {
	declare FILENAME=$1
	declare URL=$2
	touch $FILENAME

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

	# tell the user that some file is downloaded
	if [ "$LAST_WGET_DOWNLOAD_RET" -eq 0 ]; then
		echo "$FILENAME downloaded"
	else
		echo "$FILENAME failed to download"
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

for URL in $IMAGE_URLS; do 
	# extract filename 
	declare FILENAME="$(echo "$URL" | sed -E 's/.*\/([^\/]+)/\1/' )"

	# check if file exists; if do, skip it
	if [ -e "$FILENAME" ]; then
		continue
	fi

	# wait while there are too many downloading in parallel
	while [ "${#JOBS[@]}" -ge "$MAX_JOB_COUNT" ]; do 
		update-jobs
		sleep 1;
	done

	# download the file with auto-retrying
	download-with-auto-retry $FILENAME $URL &
	JOBS+=("$!")
done;

# do not exit the program before all the jobs done
while [ -n "$JOBS" ]; do 
	# echo waiting for "${JOBS[@]}"
	update-jobs;
done;

rm cover_page.html
