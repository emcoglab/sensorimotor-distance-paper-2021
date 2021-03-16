from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

# noinspection PyProtectedMember
from nltk.corpus.reader import NOUN
from numpy import zeros, corrcoef, ix_, exp, fill_diagonal, transpose, array, dot
from numpy.random import permutation
from pandas import read_csv
from scipy.spatial import distance_matrix
from scipy.spatial.distance import squareform
from scipy.stats import percentileofscore
from sklearn.metrics.pairwise import cosine_distances

from aux import find_indices, lsa_dir
from buchanan import BUCHANAN_FEATURE_NORMS
from sensorimotor_norms.sensorimotor_norms import SensorimotorNorms
from spose import SPOSE
from wordnet import WordnetAssociation


class LabelledSymmetricMatrix:
    def __init__(self, matrix: array, labels: List[str]):
        self.matrix: array = matrix
        self.labels: List[str] = labels

        assert len(labels) == matrix.shape[0] == matrix.shape[1]

    def for_subset(self, subset_words: List[str]) -> LabelledSymmetricMatrix:
        idxs = find_indices(self.labels, subset_words)
        assert len(idxs) == len(subset_words)
        return LabelledSymmetricMatrix(
            matrix=self.matrix[ix_(idxs, idxs)],
            labels=subset_words)

    def correlate_with(self, other: LabelledSymmetricMatrix) -> float:
        assert len(self.labels) == len(other.labels)
        return corrcoef(squareform(self.matrix), squareform(other.matrix))[0, 1]


class SimilarityMatrix(LabelledSymmetricMatrix):

    def for_subset(self, subset_words: List[str]) -> SimilarityMatrix:
        s = super().for_subset(subset_words)
        return SimilarityMatrix(
            matrix=s.matrix,
            labels=s.labels
        )

    @classmethod
    def by_dotproduct(cls, data_matrix: array, labels: List[str]) -> SimilarityMatrix:
        return cls(
            matrix=dot(data_matrix, transpose(data_matrix)),
            labels=labels)

    @classmethod
    def mean_softmax_probability_matrix(cls,
                                        from_similarity_matrix: SimilarityMatrix,
                                        subset_labels: Optional[List[str]] = None
                                        ) -> SimilarityMatrix:
        if subset_labels is None:
            subset_labels = from_similarity_matrix.labels
        idxs = find_indices(from_similarity_matrix.labels, subset_labels)
        assert len(idxs) == len(subset_labels)  # make sure we're not missing anything

        exp_similarity_matrix = exp(from_similarity_matrix.matrix)
        cp = zeros((len(from_similarity_matrix.labels), len(from_similarity_matrix.labels)))
        # Hebart et al.'s original code builds the entire matrix for all conditions, then selects out the relevant
        # entries. We can hugely speed up this process by only computing the entries we'll eventually select out.
        for i in idxs:
            # print_progress(i, n_all_conditions, prefix=prefix)
            for j in idxs:
                if i == j: continue
                ctmp = zeros((1, len(from_similarity_matrix.labels)))
                for k in idxs:
                    # Only interested in distinct triplets
                    if (k == i) or (k == j):
                        continue
                    ctmp[0, k] = (
                            exp_similarity_matrix[i, j]
                            / (
                                    exp_similarity_matrix[i, j]
                                    + exp_similarity_matrix[i, k]
                                    + exp_similarity_matrix[j, k]
                            ))
                cp[i, j] = ctmp.sum()
        # print_progress(n_all_conditions, n_all_conditions, prefix=prefix)
        # Complete average
        cp /= len(subset_labels)
        # Fill in the rest of the symmetric similarity matrix
        # cp += transpose(cp)  # No longer need to do this now we're filling in both sides of the matrix in the above loop
        # Instead we fix rounding errors by forcing symmetry
        cp += transpose(cp); cp /= 2
        fill_diagonal(cp, 1)
        # Select out words of interest
        selected_similarities = cp[ix_(idxs, idxs)]
        return SimilarityMatrix(matrix=selected_similarities, labels=subset_labels)

    @staticmethod
    def from_rdm(rdm: RDM) -> SimilarityMatrix:
        return SimilarityMatrix(matrix=1-rdm.matrix, labels=rdm.labels)


class RDM(LabelledSymmetricMatrix):

    def for_subset(self, subset_words: List[str]) -> RDM:
        s = super().for_subset(subset_words)
        return RDM(
            matrix=s.matrix,
            labels=s.labels
        )

    @staticmethod
    def from_similarity_matrix(similarity_matrix: SimilarityMatrix) -> RDM:
        return RDM(matrix=1 - similarity_matrix.matrix, labels=similarity_matrix.labels)

    def correlate_with_nhst(self, other: LabelledSymmetricMatrix, n_perms: int) -> Tuple[float, float]:
        r_value = self.correlate_with(other)
        p_value = randomisation_p(rdm_1=self.matrix, rdm_2=other.matrix, observed_r=r_value, n_perms=n_perms)
        return r_value, p_value


def randomisation_p(rdm_1, rdm_2, observed_r, n_perms):
    """
    Compute a p-value by randomisation test.

    Under H0, condition labels are exchangeable. Simulate null distribution of r-values by permuting labels of one RDM
    and recomputing r. Then the p-value is the fraction of the distribution above the observed r.

    :param rdm_1, rdm_2: The two RDMs to be correlated
    :param observed_r: The correlation already observed
    :param n_perms: The number of prmutations to perform
    :return:
    """
    r_perms = zeros(n_perms)
    c1 = squareform(rdm_1)
    for perm_i in range(n_perms):
        # if perm_i % 1000 == 0: print(perm_i)
        perm = permutation(rdm_1.shape[0])
        r_perms[perm_i] = corrcoef(
            c1,
            squareform(rdm_2[ix_(perm, perm)])
        )[0, 1]
    p_value = 1 - (percentileofscore(r_perms, observed_r) / 100)
    return p_value


def compute_wordnet_sm(association_type: WordnetAssociation):
    n_words = len(SPOSE.words_select_48)
    similarity_matrix = zeros((n_words, n_words))
    for i in range(n_words):
        for j in range(n_words):
            similarity_matrix[i, j] = association_type.association_between(
                word_1=SPOSE.words_select_48[i], word_1_pos=NOUN,
                word_2=SPOSE.words_select_48[j], word_2_pos=NOUN)
    fill_diagonal(similarity_matrix, 1)
    return SimilarityMatrix(matrix=similarity_matrix, labels=SPOSE.words_select_48)


def compute_lsa_sm():
    similarity_matrix_df = read_csv(Path(lsa_dir, "hebart48-lsa.csv"), header=0, index_col=0)
    similarity_matrix = similarity_matrix_df[SPOSE.words_lsa_46].loc[SPOSE.words_lsa_46].to_numpy(dtype=float)
    return SimilarityMatrix(matrix=similarity_matrix, labels=SPOSE.words_lsa_46)


def compute_buchanan_sm():
    n_words = len(SPOSE.words_common_18)
    similarity_matrix = zeros((n_words, n_words))
    for i in range(n_words):
        for j in range(n_words):
            similarity_matrix[i, j] = BUCHANAN_FEATURE_NORMS.distance_between(SPOSE.words_common_18[i], SPOSE.words_common_18[j])
    fill_diagonal(similarity_matrix, 1)
    return SimilarityMatrix(matrix=similarity_matrix, labels=SPOSE.words_common_18)


def compute_sensorimotor_rdm(distance_type) -> RDM:
    sm_data = SensorimotorNorms().matrix_for_words(SPOSE.words_select_48)
    if distance_type == DistanceType.cosine:
        rdm = cosine_distances(sm_data)
    elif distance_type == DistanceType.Minkowski3:
        rdm = distance_matrix(sm_data, sm_data, p=3)
    else:
        raise NotImplementedError()

    return RDM(matrix=rdm, labels=SPOSE.words_select_48)