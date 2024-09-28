This is an simple bash script to download galleries from nhentai, all the downloaded galleries will be stored at ~/nh/{the six digits code} folder.

There is a known issue with downloading same image for multiple times, just simply use 
`find ~/nh -type f -name "*.[1|2|3|4|5|6|7|8|9]*" -delete` to delete those files.
