from logging import getLogger, basicConfig, INFO
from pathlib import Path
from typing import Tuple, Optional, Callable, Dict

from nltk.corpus import wordnet_ic, wordnet
# noinspection PyProtectedMember
from nltk.corpus.reader import WordNetError, NOUN, VERB, ADJ, ADV
from numpy import inf
from pandas import DataFrame, read_csv, merge

from linguistic_distributional_models.evaluation.association import MenSimilarity, WordsimAll, \
    SimlexSimilarity, WordAssociationTest, RelRelatedness, RubensteinGoodenough, MillerCharlesSimilarity
from linguistic_distributional_models.utils.logging import print_progress
from linguistic_distributional_models.utils.maths import DistanceType, distance
from sensorimotor_norms.exceptions import WordNotInNormsError
from sensorimotor_norms.sensorimotor_norms import SensorimotorNorms


_logger = getLogger(__name__)
_FROM_SCRATCH = True


def load_nelson_data():
    with Path(Path(__file__).parent, "data", "Nelson_AppendixB.csv").open() as nelson_file:
        nelson = read_csv(nelson_file, skip_blank_lines=True, header=0)
    nelson["Targets"] = nelson["Targets"].str.lower()
    nelson["Part of Speech"] = nelson["Part of Speech"].str.lower()
    return nelson


NELSON = load_nelson_data()


def load_jcn_data() -> DataFrame:
    jcn_path = Path(Path(__file__).parent, "data", "Maki-BRMIC-2004", "usfjcnlsa.csv")
    with open(jcn_path) as jcn_file:
        jcn_data: DataFrame = read_csv(jcn_file)
    jcn_data.rename(columns={"#CUE": "CUE"}, inplace=True)
    return jcn_data


def add_sensorimotor_predictor(dataset: DataFrame, word_key_cols: Tuple[str, str], distance_type: DistanceType):
    predictor_name = f"Sensorimotor distance ({distance_type.name})"
    if predictor_name in dataset.columns:
        _logger.info("Predictor already exists, skipping")
        return
    key_col_1, key_col_2 = word_key_cols
    sn = SensorimotorNorms()

    i = 0
    n = dataset.shape[0]

    def calc_sensorimotor_distance(row):
        nonlocal i
        i += 1
        print_progress(i, n, prefix=f"Sensorimotor {distance_type.name}: ", bar_length=200)
        try:
            v1 = sn.vector_for_word(row[key_col_1])
            v2 = sn.vector_for_word(row[key_col_2])
            return distance(v1, v2, distance_type=distance_type)
        except WordNotInNormsError:
            return None

    # noinspection PyTypeChecker
    dataset[predictor_name] = dataset.apply(calc_sensorimotor_distance, axis=1)


def add_wordnet_predictor(dataset: DataFrame, word_key_cols: Tuple[str, str], pos_filename: str):
    predictor_name = "WordNet distance (JCN)"
    if predictor_name in dataset.columns:
        _logger.info("Predictor already exists, skipping")
        return
    key_col_1, key_col_2 = word_key_cols

    brown_ic = wordnet_ic.ic('ic-brown.dat')

    elex_to_wordnet = {
        "nn": NOUN,
        "vb": VERB,
        "jj": ADJ,
        "rb": ADV,
    }
    elex_pos: Dict[str, str]
    if pos_filename:
        with Path(Path(__file__).parent, "data", "elexicon", pos_filename).open("r") as pos_file:
            elex_df = read_csv(pos_file, header=0, index_col=None, delimiter="\t")
            elex_df.set_index("Word", inplace=True)
            elex_dict: dict = elex_df.to_dict('index')
            elex_pos = {
                word: [
                    elex_to_wordnet[pos.lower()]
                    for pos in data["POS"].split("|")
                    if pos in elex_to_wordnet
                ]
                for word, data in elex_dict.items()
            }
    else:
        elex_pos = dict()

    i = 0
    n = dataset.shape[0]

    def get_pos(word) -> Optional[str]:
        if elex_pos is None:
            return None
        try:
            # Assume Elexicon lists POS in precedence order
            return elex_pos[word][0]
        except KeyError:
            return None
        except IndexError:
            return None

    def calc_jcn_distance(row):
        nonlocal i
        i += 1
        print_progress(i, n, prefix="WordNet Jiang–Coranth: ", bar_length=200)

        # Get words
        w1 = row[key_col_1]
        w2 = row[key_col_2]

        # Get POS
        pos_1 = get_pos(w1)
        pos_2 = get_pos(w2)
        # if pos_1 != pos_2:
        #     Can only compute distances between word pairs of the same POS
            # return None

        # Get JCN
        try:
            synsets1 = wordnet.synsets(w1, pos=pos_1)
            synsets2 = wordnet.synsets(w2, pos=pos_2)
        except WordNetError:
            return None
        minimum_jcn_distance = inf
        for synset1 in synsets1:
            for synset2 in synsets2:
                try:
                    jcn = 1 / synset1.jcn_similarity(synset2, brown_ic)  # Match the formula of Maki et al. (2004)
                    minimum_jcn_distance = min(minimum_jcn_distance, jcn)
                except WordNetError:
                    # Skip incomparable pairs
                    continue
                except ZeroDivisionError:
                    # Similarity was zero/distance was infinite
                    continue
        if minimum_jcn_distance >= 1_000:
            return None
        return minimum_jcn_distance

    # noinspection PyTypeChecker
    dataset[predictor_name] = dataset.apply(calc_jcn_distance, axis=1)


def add_lsa_predictor(data, word_key_cols, lsa_filename):
    predictor_name = "LSA"
    if predictor_name in data.columns:
        _logger.info("Predictor already exists, skipping")
        return

    with Path(Path(__file__).parent, "data", "LSA", lsa_filename).open("r") as lsa_file:
        lsa_deets: DataFrame = read_csv(lsa_file, header=None)
    lsa_deets.columns = [*word_key_cols, predictor_name]
    data = merge(data, lsa_deets, on=list(word_key_cols), how="left")
    return data


def process(out_dir: str,
            out_file_name: str,
            load_from_source: Callable[[], DataFrame],
            word_key_cols: Tuple[str, str],
            pos_filename: Optional[str],
            lsa_filename: Optional[str],
            ):
    _logger.info(out_file_name)

    data_path = Path(out_dir, out_file_name)
    data: DataFrame
    if _FROM_SCRATCH or not data_path.exists():
        _logger.info("Loading from source")
        data = load_from_source()
    else:
        _logger.info("Loading previously saved file")
        with data_path.open(mode="r") as data_file:
            data = read_csv(data_file, header=0, index_col=None)

    if pos_filename is not None:
        _logger.info("Adding WordNet JCN predictor")
        add_wordnet_predictor(data, word_key_cols, pos_filename)
        _logger.info("Saving")
        with data_path.open(mode="w") as out_file:
            data.to_csv(out_file, header=True, index=False)

    if lsa_filename is not None:
        _logger.info("Adding LSA predictor")
        data = add_lsa_predictor(data, word_key_cols, lsa_filename)
        _logger.info("Saving")
        with data_path.open(mode="w") as out_file:
            data.to_csv(out_file, header=True, index=False)

    _logger.info("Adding sensorimotor predictor")
    add_sensorimotor_predictor(data, word_key_cols, distance_type=DistanceType.Minkowski3)
    add_sensorimotor_predictor(data, word_key_cols, distance_type=DistanceType.cosine)
    # add_sensorimotor_predictor(data, word_key_cols, distance_type=DistanceType.correlation)
    # add_sensorimotor_predictor(data, word_key_cols, distance_type=DistanceType.Euclidean)
    _logger.info("Saving")
    with data_path.open(mode="w") as out_file:
        data.to_csv(out_file, header=True, index=False)

    _logger.info("")


if __name__ == '__main__':

    basicConfig(format='%(asctime)s | %(levelname)s | %(module)s | %(message)s', datefmt="%Y-%m-%d %H:%M:%S", level=INFO)

    save_dir = Path("/Users/caiwingfield/Resilio Sync/Lancaster/CogSci 2021/")

    process(save_dir, "rg.csv",
            lambda: RubensteinGoodenough().associations_to_dataframe(),
            (WordAssociationTest.TestColumn.word_1, WordAssociationTest.TestColumn.word_2),
            pos_filename="rg-pos.tab",
            lsa_filename="rg-lsa.csv")
    process(save_dir, "miller_charles.csv",
            lambda: MillerCharlesSimilarity().associations_to_dataframe(),
            (WordAssociationTest.TestColumn.word_1, WordAssociationTest.TestColumn.word_2),
            pos_filename="miller-charles-pos.tab",
            lsa_filename="miller-charles-lsa.csv")
    process(save_dir, "rel.csv",
            lambda: RelRelatedness().associations_to_dataframe(),
            (WordAssociationTest.TestColumn.word_1, WordAssociationTest.TestColumn.word_2),
            pos_filename="rel-pos.tab",
            lsa_filename="rel-lsa.csv")
    process(save_dir, "wordsim.csv",
            lambda: WordsimAll().associations_to_dataframe(),
            (WordAssociationTest.TestColumn.word_1, WordAssociationTest.TestColumn.word_2),
            pos_filename="wordsim-pos.tab",
            lsa_filename="wordsim-lsa.csv")
    process(save_dir, "simlex.csv",
            lambda: SimlexSimilarity().associations_to_dataframe(),
            (WordAssociationTest.TestColumn.word_1, WordAssociationTest.TestColumn.word_2),
            pos_filename="simlex-pos.tab",
            lsa_filename="simlex-lsa.csv")
    process(save_dir, "men.csv",
            lambda: MenSimilarity().associations_to_dataframe(),
            (WordAssociationTest.TestColumn.word_1, WordAssociationTest.TestColumn.word_2),
            pos_filename="men-pos.tab",
            lsa_filename="men-lsa.csv")
    process(save_dir, "jcn.csv",
            lambda: load_jcn_data(),
            ("CUE", "TARGET"),
            pos_filename="jcn-pos.tab",
            lsa_filename=None)
    # process(save_dir, "swow_r1.csv", lambda: SmallWorldOfWords(responses_type=SmallWorldOfWords.ResponsesType.R1).associations_to_dataframe(), (WordAssociationTest.TestColumn.word_1, WordAssociationTest.TestColumn.word_2), "n", lsa_filename=None)
    # process(save_dir, "swow_r123.csv", lambda: SmallWorldOfWords(responses_type=SmallWorldOfWords.ResponsesType.R123).associations_to_dataframe(), (WordAssociationTest.TestColumn.word_1, WordAssociationTest.TestColumn.word_2), "n", lsa_filename=None)

    _logger.info("Done!")