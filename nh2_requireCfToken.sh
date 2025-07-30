#!/bin/bash

# constants
VERSION="2024-10-03"

# kill the whole process group of this script on Ctrl + C
# ref: https://stackoverflow.com/a/2173421
trap 'trap - SIGTERM && kill -- -$$' SIGINT SIGTERM

# utilities
errcho() { echo "$@" 1>&2; }

print_help() {
	echo "This is a script used to download an nhentai as pictures"
	echo
	echo "Usage: (./)nh2.sh [OPTIONS...] [NUMBERS...|help]"
	echo 
	echo "NUMBERS: The magic numbers of the galleries (i.e., book IDs)."
	echo
	echo "OPTIONS:"
	echo '  -c, --cookie="name1=value1; name2=value2"'
    echo '                                Provides a cookie string to bypass CAPTCHA.'
	echo '  -a, --user-agent="UA"         Provides a UA string to bypass CAPTCHA.'
	echo "  -r, --max-retry=NUMBER        Times to retry downloading after failure."
	echo "                                (default: 20)"
	echo '  -m, --media-server-list="SERVER1 SERVER2 ..."'
	echo "                                List of alternative servers, space seperated."
	echo "                                Used sequentially when retrying."
	echo '                                (default: "3 7 5")'
	echo '                                (Hint: "dig i$''{NUM}.nhentai.net" to test)'
	echo '  -f, --folder-path=PATH        Specify a directory for image stroage.'
	echo '                                (default: ~/nh)'
	echo '  -p, --parallel[=MAX_JOBS]     Max number of download jobs in parallel.'
	echo '                                (default: 1 if not specified, 20 if presenting)'
	echo '  -h, --help                    Show this message. (You may as well use the '
	echo '                                keyword help to print out this help messege)'
	echo '  -v, --version                 Show the version.'
}

throw() {
	while [ "$#" -gt 0 ]; do 
		errcho "$1"
		shift 
	done 
	finish 1;
}

throwhelp() {
	throw "$@" \
		"" \
		"--------------------------------------------------------------------------------" \
		"$(print_help)"
}

# a job management system to ensure the script only exit 
# when all the things are done
declare JOBS=()
update_jobs() {
	declare UPDATED=()
	for PID in "${JOBS[@]}"; do 
		if ps -p "$PID" >> /dev/null ; then
			UPDATED+=("$PID")
		fi
	done;
	JOBS=("${UPDATED[@]}")
}
wait_for_jobs() {
	while [ -n "${JOBS[*]}" ]; do 
		# echo waiting for "${JOBS[@]}"
		update_jobs;
	done;
}
finish() { 
	declare EXIT_CODE
	EXIT_CODE="${1:-0}"
	update_jobs;

	# wait for jobs, if there is any undone
	if [ -n "${JOBS[*]}" ]; then
		echo "Waiting for jobs to finish..."
		wait_for_jobs; 
		# after the waiting, error message may have been flushed away. 
		# notify the user to check them
		if [ "$EXIT_CODE" -ne "0" ]; then 
			echo "Something went wrong with the script (exit $EXIT_CODE). Check the error above."
		fi
	fi

	exit "$EXIT_CODE"; 
}

# a tool for parsing the arguments 
parse_args() {
	if [ "$#" -le 4 ]; then 
		echo "Illegal use of parse_args. You should give at least three arguments: "
		echo "  - A regex matches options with values (e.g. '^--(output|etc)|-[oe]$')"
		echo "  - A regex matches options w/o values (e.g. '^--(help|version)|-[hv]$')"
		echo "  - A regex matches options with optional value (e.g. '^--(retry|parallel)|-[rp]$')"
		echo "    - note the '^' and '$' at the two ends of the regex"
		echo "  - A callback command, which takes 4 parameter: "
		echo "    - status (NO_VAL, WITH_VAL, UNEXPT_VAL, UNEXPT_NO_VAL, UNKNOWN_OPT, NON_OPT, OPT_VAL_NO_EQ)"
		echo '    - option name: you must implement for names `''PARSE_ARGS_NON_OPTION`'
		echo '    - option value: (if given)'
		echo '    - full error argument'
		echo '  - Pass all the arugments to parse with "$@". You must check if "$@" is empty in advance.'
		finish 1
	fi

	declare OPTS_WITH_VALUE="$1"
	declare OPTS_WO_VALUE="$2"
	declare OPTS_WITH_OPT_VALUE="$3"
	declare CALLBACK="$4"

	shift 4

	declare SIGN # the symbol of a flag, such as `+` in `+f` and `-` in `-f`
	declare FLAGS # the leftover part of an argument 
	declare ARG # the full argument
	while [ "$#" -gt 0 ]; do
		declare FLAG
		declare EQENDING

		# if the last argument is fully parsed, parse another
		if [ -z "$FLAGS" ]; then
			ARG="$1"
			shift 1

			if [[ "$ARG" =~ ^[-+][a-zA-Z0-9]+(=.*)?$ ]]; then
				SIGN="$(sed -E 's/^([-+])([a-zA-Z0-9]+)(=.*)?$/\1/' <<< "$ARG")" 
				FLAGS="$(sed -E 's/^([-+])([a-zA-Z0-9]+)(=.*)?$/\2\3/' <<< "$ARG")"
			elif [[ "$ARG" =~ ^--[-a-zA-Z0-9]+(=.*)?$ ]]; then
				FLAG="$(sed -E 's/^(--[-a-zA-Z0-9]+)(=.*)?$/\1/' <<< "$ARG")" 
				EQENDING="$(sed -E 's/^(--[-a-zA-Z0-9]+)(=.*)?$/\2/' <<< "$ARG")" 
			else 
				"$CALLBACK" NON_OPT "" "$ARG" 
				continue
			fi
		fi

		if [ -n "$FLAGS" ]; then
			declare FLAG_REST="${FLAGS#?}"
			FLAG="$SIGN${FLAGS%"$FLAG_REST"}"
			if [[ "$FLAG_REST" =~ ^=.* ]]; then
				EQENDING=${FLAG_REST#=}
				FLAGS=""
			else 
				FLAGS="${FLAG_REST}"
			fi
		fi

		if [[ "$FLAG" =~ $OPTS_WITH_OPT_VALUE ]]; then
			# for a option with optional value
			if [ -z "$EQENDING" ]; then # the next symbol is not '='
				if { [[ -n "$FLAGS" ]] && [[ ! "$FLAGS" =~ ^[a-zA-Z0-9].* ]]; } then 
					# if the next symbol is not a valid flag, treat it as invalid value 
					"$CALLBACK" OPT_VAL_NO_EQ "$FLAG" "$FLAGS" "$ARG"
				else 
					"$CALLBACK" NO_VAL "$FLAG"
				fi
			else # pass the value if there is a value
				"$CALLBACK" WITH_VAL "$FLAG" "${EQENDING#=}"
			fi
		elif [[ "$FLAG" =~ $OPTS_WITH_VALUE ]]; then
			# for options with value
			if [ -n "$FLAGS" ]; then # treat text after it as value
				"$CALLBACK" WITH_VAL "$FLAG" "$FLAGS"
			elif [ -n "$EQENDING" ]; then # remove the '=', and treat it as value
				"$CALLBACK" WITH_VAL "$FLAG" "${EQENDING#=}"
			elif [ "$#" -gt 0 ]; then # treat the next argument as value
				"$CALLBACK" WITH_VAL "$FLAG" "$1"
				shift 1
			else # send the exception of no value
				"$CALLBACK" UNEXPT_NO_VAL "$FLAG" "" "$ARG"
			fi
		elif [[ "$FLAG" =~ $OPTS_WO_VALUE ]]; then
			if { [ -z "$FLAGS" ] && [ -n "$EQENDING" ] ; }; then 
				# a equal sign on option without value is wrong
				"$CALLBACK" UNEXPT_VAL "$FLAG" "${EQENDING#=}" "$ARG"
			elif { [ -n "$FLAGS" ] && [[ ! "$FLAGS" =~ ^[a-zA-Z0-9].* ]] ; } ; then
				# if the next symbol is not a valid flag, treat it as invalid value 
				"$CALLBACK" UNEXPT_VAL "$FLAG" "$FLAGS" "$ARG"
			else # call back with no value 
				"$CALLBACK" NO_VAL "$FLAG"
			fi
		else 
			"$CALLBACK" UNKNOWN_OPT "$FLAG" "" "$ARG"
		fi

		FLAG=
		EQENDING=
	done
}

# == START OF parse the arguments ==
declare MAX_JOB_COUNT=1
declare MAX_RETRY=5
declare MEDIA_SERVER_LIST=(3 7 5)
declare ID_LIST=()
declare FOLDER_PATH="$HOME/nh"
declare COOKIE=""
declare UA=""
argument_callback() {
	declare STATUS="$1"
	declare FLAG="$2"
	declare VALUE="$3"
	declare ERR_ARG="$4"
	case "$1" in 
		UNEXPT_VAL)
			throwhelp "Option '$FLAG' doesn't require a value '$VALUE'. Error at '$ERR_ARG'.";;
		UNEXPT_NO_VAL)
			throwhelp "Option '$FLAG' requires a value. Error at '$ERR_ARG'.";;
		UNKNOWN_OPT)
			throwhelp "Option '$FLAG' is unknown. Error at '$ERR_ARG'." \
				"Hint: if this is a optional value of the previous option (such a '-p')," \
				"      you may need to specify it with equal sign '='." ;;
		OPT_VAL_NO_EQ)
			throwhelp "The optional value of option '$FLAG' must be explicitly specified with equal sign '='." \
				"Error at '$ERR_ARG'" \
				"Hint: your value '$VALUE' may be a wrongly-typed flag." ;;
		NON_OPT)
			if [ "$VALUE" = "help" ]; then
				print_help 
				finish
			elif [[ "$VALUE" =~ [0-9]+ ]]; then
				ID_LIST+=("$VALUE")
				return
			else 
				throwhelp "'$VALUE' is neither a book id, an option, or a keyword like 'help'."
			fi;;
	esac
	case "$2" in 
		--help|-h)
			print_help; finish;;
		--version|-v)
			echo "$VERSION"; finish;;
		--max-retry|-r) 
			MAX_RETRY="$VALUE";;
		--media-server-list|-m)
			# MEDIA_SERVER_LIST=($VALUE);;
			IFS=" " read -r -a MEDIA_SERVER_LIST <<< "$VALUE" ;;
		--folder-path|-f)
			FOLDER_PATH="${VALUE/#\~/$HOME}";;
				# expand '~' to "$HOME" correctly 
				# ref: https://stackoverflow.com/a/27485157
		--parallel|-p) 
			case "$STATUS" in 
				NO_VAL)
					MAX_JOB_COUNT=20;;
				WITH_VAL)
					MAX_JOB_COUNT="$VALUE";;
				*)
					throw "Unexpected status. Status '$STATUS', option '$FLAG', value '$VALUE', ERR_ARG '$ERR_ARG'";;
			esac;;
        --cookie|-c)
            COOKIE="$VALUE";;
        --user-agent|-a)
            UA="$VALUE";;
		*) 
			throw "Unexpected things happened. This line shouldn't be executed." \
				"Option '$FLAG', value '$VALUE', ERR_ARG '$ERR_ARG'";;
	esac
}

if [ -z "$*" ]; then
    print_help
    finish
fi

parse_args '^--(max-retry|media-server-list|folder-path|cookie|user-agent)|-[rmfca]$' '^--(help|version)|-[hv]$' '^--(parallel)|-[p]$' argument_callback "$@"

# if there is no given book id, shows error
if [ -z "${ID_LIST[*]}" ]; then 
	throwhelp "At least one book id should be given. "
fi

# if no cookie given, shows a warning 
if [ -z "${COOKIE}" ] || [ -z "${UA}" ] ; then
    echo "(WARN) No cookie and UA given, so the download may fail at CAPTCHA. Consider to"
    echo "  1. Open https://nhentai.net in browser"
    echo "  2. Solve the cloudflare CAPTCHA (if presents)"
    echo '  3. Open DevTools, and switch to the "Network" tab'
    echo "  4. Hard-reload the page by pressing [Enter] in the browser's URL bar"
    echo '  5. Find the request to https://nhentai.net/,'
    echo '     and copy the Request Headers named "Cookie" and "User-Agent"'
    echo '     (Tip: Use the filter) (Cookie should contain cf_clearance=...)'
    echo '  6. Provides them in the command with --cookie/-c and --user-agent/-a'
    echo ""
fi

# == END OF parse the arguments ==

# a command to download with auto-retrying
# we'll use it in main()
download_with_auto_retry() {
	declare FILENAME="$1"
	declare URL="$2"
	# touch "$FILENAME"

	wget -q -O "$FILENAME" "$URL"
	declare LAST_WGET_DOWNLOAD_RET=$?
		# by using `dig i${n}.nhentai.net`, we can see only these three
		# servers have IPv4 addresses and are thus valid

	for i in $(seq 1 "$MAX_RETRY"); do 
		# retry if wget didn't run successfully
		if [ "$LAST_WGET_DOWNLOAD_RET" -eq 0 ]; then
			break;
		fi 
		declare ALTER_MEDIA_SERVER_IDX=$(((i - 1) % ${#MEDIA_SERVER_LIST[@]}))
		declare ALTER_MEDIA_SERVER=${MEDIA_SERVER_LIST[ALTER_MEDIA_SERVER_IDX]}
		declare ALTER_URL
		ALTER_URL=$(echo "$URL" | sed -E "s/\/\/(i|t)[0-9]*\./\/\/\1${ALTER_MEDIA_SERVER}./")
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

# == START OF main ==
# switch to the directory for storage
if [ ! -d "$FOLDER_PATH" ]; then 
	mkdir -p "$FOLDER_PATH" || throw "Failed to create the directory for storage, '$FOLDER_PATH'"
fi
cd "$FOLDER_PATH" || throw "Failed to switch to the directory for storage, '$FOLDER_PATH'" 

for ID in "${ID_LIST[@]}"; do 
	mkdir "$ID" || throw "Failed to create the directory for book#$ID"

	# fetch the cover page and save it
	echo "Parsing book#$ID..."
	declare COVER_HTML
    declare COVER_WGET_EXTRA_ARGS=()

    if [ -n "$COOKIE" ]; then
        COVER_WGET_EXTRA_ARGS+=(--no-cookies --header="Cookie: ${COOKIE}")
    fi

    if [ -n "$UA" ]; then
        COVER_WGET_EXTRA_ARGS+=(--header="User-Agent: ${UA}")
    fi
    
    COVER_HTML="$(wget -O - "https://nhentai.net/g/$ID/" "${COVER_WGET_EXTRA_ARGS[@]}")"

	echo "$COVER_HTML" > "$ID/cover_page.html"
	
	# extract page count for automatic parallel adjustment
	declare PAGE_COUNT
	PAGE_COUNT="$(echo "$COVER_HTML" | grep "<span class=\"tags\"><a class=\"tag\" href=\"/search/?q=pages" | grep -oEe "<span class=\"name\">[0-9]*" | grep -oEe "[0-9]*")"
	
	MAXIMUM=100

	# adjust MAX_JOB_COUNT based on page count if not manually set
	if [ -n "$PAGE_COUNT" ] && [ "$PAGE_COUNT" -gt 0 ]; then
		# only use auto-detected value if user didn't specify parallel option
		if [ "$MAX_JOB_COUNT" -eq 1 ]; then
			MAX_JOB_COUNT=$(( PAGE_COUNT < MAXIMUM ? PAGE_COUNT : MAXIMUM ))
			echo "Auto-detected $PAGE_COUNT pages, setting parallel jobs to $MAX_JOB_COUNT"
		fi
	fi
	# extract a list of images that we need to download
	# make enter after each html tag 
	# -> grep the urls 
	#        - pattern: cover.jpg/png/gif, and t${number}.jpg/png/gif for thumbnails
	# -> convert thumbnail filenames to normal files
	# -> uniquify the links with awk
	#    - notice: there may still be multiple urls for book covers
	declare IMAGE_URLS
	IMAGE_URLS="$(echo "$COVER_HTML" \
		| sed -E 's/>/\n/g' \
		| grep -oEe '//t[0-9]+.nhentai\.net/galleries/[0-9]+/[0-9]+t\.[a-zA-Z]+' \
		| sed -E 's/t(\.[a-zA-Z]{1,10})$/\1/g' | sed -E 's/\/\/t([0-9]+)\./\/\/i\1./' \
		| awk '!a[$0]++' \
		| sed 's|^|https:|'
	)"

	# echo $IMAGE_URLS

	for URL in $IMAGE_URLS; do 
		# extract filename 
		declare FILENAME
		FILENAME="$ID/$(echo "$URL" | sed -E 's/.*\/([^\/]+)/\1/' )"

		# check if file exists; if do, skip it
		if [ -e "$FILENAME" ]; then
			continue
		fi

		# wait while there are too many downloading in parallel
		while [ "${#JOBS[@]}" -ge "$MAX_JOB_COUNT" ]; do 
			update_jobs
			sleep 1;
		done

		# download the file with auto-retrying
		download_with_auto_retry "$FILENAME" "$URL" &
		JOBS+=("$!")
	done
done

wait_for_jobs

# == END OF main ==
