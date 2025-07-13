This is a simple bash script to download galleries from nhentai, \
all the downloaded galleries will be stored at ~/nh/{the six digits code} folder, \
Note that this script will not `mkdir ~/nh` for you, so you'll have to do it yourself or it'll be stored at your current directory

For Windows users or who just want to use docker, simply build with Dockerfile we provided `docker build -t nh-project` and run `docker run --rm -v "${HOME}/nh:/root/nh" nh-project`, it will run `bash download.sh nhentai.txt` for you, or run interactively with `docker run --rm -it -v "${HOME}/nh:/root/nh" nh-project bash`
