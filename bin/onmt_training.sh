#!/usr/bin/env bash

# dir of the actual script
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Default values
TRAIN="."
DEV="."
CONFIG="."
FIELDS="deprel"
WRITE="false"
USE="cli"
BUILD='false'

# handling of the inputs for the bash file
help(){
    echo "Usage onmt_training - it aims to build the dataset and the vocab before training the model according to the config file
            [-t | --train] : path to the .conllu training file, by default=$TRAIN
            [-d | --dev] : path to the .conllu validation file, by default=$DEV
            [-c | --config] : config file .yaml format used for the training, by default=$CONFIG
            [-f | --fields] : fields of the conllu file to be used for validation, can use multiple ones separated by '-', by default=$FIELDS -> upos /xpos / feats / head / deprel / deps
            [-w | --write] : write in the config file if it is given, by default=$WRITE -> true / false
            [-u | --use] : choose to build the dataset according to the cli or the config file, by default=$USE -> cli / config
            [-b | --build] : choose to only build the dataset before training, default=$BUILD -> true / false
    "
    exit 2
}

# Options
SHORT=t:,d:,c:,f:,w:,u:,b:,h
LONG=train:,dev:,config:,fields:,write:,use:,build:,help
OPTS=$(getopt -a -n onmt_training --options $SHORT --longoptions $LONG -- "$@")

VALID_ARGUMENTS=$# # Returns the count of arguments that are in short or long options

if [ "$VALID_ARGUMENTS" -eq 0 ]; then
    echo "You have to enter at least on argument" 
    help
fi

eval set -- "$OPTS"

while :
do
  case "$1" in
    -t | --train )
      TRAIN="$2"
      shift 2
      ;;
    -d | --dev )
      DEV="$2"
      shift 2
      ;;
    -c | --config )
      CONFIG="$2"
      shift 2
      ;;
    -f | --fields )
      FIELDS="$2"
      shift 2
      ;;
    -w | --write )
      WRITE="$2"
      shift 2
      ;;
    -u | --use )
      USE="$2"
      shift 2
      ;;
    -b | --build )
      BUILD="$2"
      shift 2
      ;;
    -h | --help)
      help
      exit 2
      ;;
    --)
      shift;
      break 
      ;;
    *)
      echo "Unexpected option : $1"
      help
      exit 2
      ;;
  esac
done


# building the dataset based on the paths given
python "$SCRIPT_DIR/build_dataset.py -t $TRAIN -d $DEV -c $CONFIG -f $FIELDS -w $WRITE -u $USE" || exit

# building the vocab based on the path given on the config file
echo "INFO - BUILDING VOCAB"
N_CPUS=$(lscpu |awk -v skip=6 '{for (i=2;i<skip;i++) {getline}; print $0}' |awk '{print $2}' |head -n 1)
onmt_build_vocab -config $CONFIG -n_sample -1 -num_threads $N_CPUS

# training the model
if [ $BUILD -eq "false" ]; then
  echo "INFO - TRAINING MODEL"
  onmt_train -config $CONFIG 
fi

# enable the scripts to be launched
export PATH="$SCRIPT_DIR:$PATH"
chmod u+x "$SCRIPT_DIR/download_trankit_vocab.sh"

exit 0