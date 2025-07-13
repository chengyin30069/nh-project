This is a simple bash script to download galleries from nhentai, \
all the downloaded galleries will be stored at ~/nh/{the six digits code} folder, \
Note that this script will not `mkdir ~/nh` for you, so you'll have to do it yourself or it'll be stored at your current directory

For Windows users or who just want to use docker, simply build with Dockerfile we provided 
1. `docker build -t nh-project`
2. `docker run --rm -v "${HOME}/nh:/root/nh" nh-project` (run `bash download.sh nhentai.txt` in docker)
3. (optional) `docker run --rm -it -v "${HOME}/nh:/root/nh" nh-project bash` (run interactively with our scripts)
