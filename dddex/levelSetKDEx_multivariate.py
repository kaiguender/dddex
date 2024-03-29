# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/02_levelSetKDEx_multivariate.ipynb.

# %% ../nbs/02_levelSetKDEx_multivariate.ipynb 4
from __future__ import annotations
from fastcore.docments import *
from fastcore.test import *
from fastcore.utils import *

import pandas as pd
import numpy as np
import faiss
from scipy.spatial.distance import cdist
from scipy.optimize import linear_sum_assignment
from scipy.spatial import KDTree
from sklearn.base import BaseEstimator
from sklearn.exceptions import NotFittedError

from collections import defaultdict, Counter, deque
from joblib import Parallel, delayed, dump, load
import copy
import warnings

from .baseClasses import BaseLSx, BaseWeightsBasedEstimator_multivariate
from .wSAA import SampleAverageApproximation, RandomForestWSAA, RandomForestWSAA_LGBM
from .utils import restructureWeightsDataList_multivariate

# %% auto 0
__all__ = ['LevelSetKDEx_multivariate']

# %% ../nbs/02_levelSetKDEx_multivariate.ipynb 6
class LevelSetKDEx_multivariate(BaseWeightsBasedEstimator_multivariate, BaseLSx):
    """
    `LevelSetKDEx` turns any point forecasting model into an estimator of the underlying conditional density.
    The name 'LevelSet' stems from the fact that this approach interprets the values of the point forecasts
    as a similarity measure between samples. The point forecasts of the training samples are sorted and 
    recursively assigned to a bin until the size of the current bin reaches `binSize` many samples. Then
    a new bin is created and so on. For a new test sample we check into which bin its point prediction
    would have fallen and interpret the training samples of that bin as the empirical distribution function
    of this test sample.    
    """
    
    def __init__(self, 
                 estimator, # Model with a .fit and .predict-method (implementing the scikit-learn estimator interface).
                 binSize: int=None, # Size of the bins created while running fit.
                 # Determines behaviour of method `getWeights`. If False, all weights receive the same  
                 # value. If True, the distance of the point forecasts is taking into account.
                 equalBins: bool=False,
                 ):
        
        super(BaseEstimator, self).__init__(estimator = estimator)
        
        # Check if binSize is int
        if not isinstance(binSize, int):
            raise ValueError("'binSize' must be an integer!")
        
        # Check if equalBins is bool
        if not isinstance(equalBins, bool):
            raise ValueError("'equalBins' must be a boolean!")
        
        self.equalBins = equalBins
        self.binSize = binSize
        
        self.yTrain = None
        self.yPredTrain = None
        self.indicesPerBin = None
        self.lowerBoundPerBin = None
        self.fitted = False
    
    #---
    
    def fit(self: LevelSetKDEx, 
            X: np.ndarray, # Feature matrix used by `estimator` to predict `y`.
            y: np.ndarray, # 1-dimensional target variable corresponding to the feature matrix `X`.
            ):
        """
        Fit `LevelSetKDEx` model by grouping the point predictions of the samples specified via `X`
        according to their value. Samples are recursively sorted into bins until each bin contains
        `binSize` many samples. For details, checkout the function `generateBins` which does the
        heavy lifting.
        """
        
        # Checks
        if self.binSize is None:
            raise ValueError("'binSize' must be specified to fit the LSx estimator!")
            
        if self.binSize > y.shape[0]:
            raise ValueError("'binSize' mustn't be bigger than the size of 'y'!")
        
        if X.shape[0] != y.shape[0]:
            raise ValueError("'X' and 'y' must contain the same number of samples!")
        
        # IMPORTANT: In case 'y' is given as a pandas.Series, we can potentially run into indexing 
        # problems later on.
        if isinstance(y, pd.Series):
            y = y.ravel()
        
        #---
        
        try:
            yPred = self.estimator.predict(X)
            
        except NotFittedError:
            try:
                self.estimator.fit(X = X, y = y)                
            except:
                raise ValueError("Couldn't fit 'estimator' with user specified 'X' and 'y'!")
            else:
                yPred = self.estimator.predict(X)
        
        #---
        
        if len(y.shape) == 1:
            y = y.reshape(-1, 1)
            yPred = yPred.reshape(-1, 1)
        
        #---
        
        # Compute desired number of clusters dependend on binSize and number of samples
        nClusters = int(np.ceil(yPred.shape[0] / self.binSize))

        # Modify yPred to be compatible with faiss
        yPredMod = yPred.astype(np.float32)
        
        # Train kmeans model based on the faiss library
        kmeans = faiss.Kmeans(d = yPredMod.shape[1], k = nClusters)
        kmeans.train(yPredMod)

        # Get cluster centers created by faiss. IMPORTANT NOTE: not all clusters are used! We will handle that further below.
        centersAll = kmeans.centroids
        
        # Compute the cluster assignment for each sample
        if self.equalBins:
            clusterAssignments = self._getEqualSizedClusters(y = yPredMod)            
        else:
            clusterAssignments = kmeans.assign(yPredMod)[1]
        
        # Based on the clusters and cluster assignments, we can now compute the indices belonging to each bin / cluster
        indicesPerBin = defaultdict(list)
        binSizes = defaultdict(int)

        for index, cluster in enumerate(clusterAssignments):
            indicesPerBin[cluster].append(index)
            binSizes[cluster] += 1

        #---

        clustersUsed = np.array(list(indicesPerBin.keys()))
        clustersOrdered = np.sort(clustersUsed)

        centers = centersAll[clustersOrdered]
        indicesPerBin = [indicesPerBin[cluster] for cluster in clustersOrdered]
        binSizes = np.array([binSizes[cluster] for cluster in clustersOrdered])

        #---

        # Merge clusters that are too small (i.e. contain less than binSize / 2 samples).
        # clustersTooSmall is the array of all clusters that are too small.
        threshold = self.binSize / 2
        binsTooSmall = np.where(binSizes < threshold)[0]
        
        if len(binsTooSmall) > 0:

            # remove all centers from centersOld that are part of clustersTooSmall
            centersNew = np.delete(centers, binsTooSmall, axis = 0)
            centersTooSmall = centers[binsTooSmall]
            centersNew_oldIndices = np.delete(np.arange(len(centers)), binsTooSmall)

            KDTreeNew = KDTree(centersNew)
            clustersToMerge = KDTreeNew.query(centersTooSmall)[1]

            for i, clusterToMerge in enumerate(clustersToMerge):
                indicesPerBin[centersNew_oldIndices[clusterToMerge]].extend(indicesPerBin[binsTooSmall[i]])

            # Remove the indices given by clustersTooSmall from indicesPerBin by deleting the list entry
            indicesPerBin = [np.array(indices) for binIndex, indices in enumerate(indicesPerBin) if binIndex not in binsTooSmall]
            binSizes = [len(indices) for indices in indicesPerBin]
            binSizes = pd.Series(binSizes)

            self.centers = centersNew
            self.binSizes = binSizes
            self.kmeans = KDTreeNew
        
        else:
            self.centers = centers
            self.binSizes = pd.Series(binSizes)
            self.kmeans = KDTree(self.centers)

            # Transform the indices given by indicesPerBin into numpy arrays
            indicesPerBin = [np.array(indices) for indices in indicesPerBin]
            
        #---
        
        self.yTrain = y
        self.yPredTrain = yPred
        self.indicesPerBin = indicesPerBin
        self.fitted = True
        
        
    #---
    
    def _getEqualSizedClusters(self,
                               y,
                               ):
            
        centers = self.centers.reshape(-1, 1, y.shape[-1]).repeat(self.binSize, 1).reshape(-1, y.shape[-1])

        distance_matrix = cdist(y, centers)
        clusterAssignments = linear_sum_assignment(distance_matrix)[1]//self.binSize

        return clusterAssignments

    #---
    
    def getWeights(self, 
                   X: np.ndarray, # Feature matrix for which conditional density estimates are computed.
                   # Specifies structure of the returned density estimates. One of: 
                   # 'all', 'onlyPositiveWeights', 'summarized', 'cumDistribution', 'cumDistributionSummarized'
                   outputType: str='onlyPositiveWeights', 
                   # Optional. List with length X.shape[0]. Values are multiplied to the estimated 
                   # density of each sample for scaling purposes.
                   scalingList: list=None, 
                   ) -> list: # List whose elements are the conditional density estimates for the samples specified by `X`.
        
        # __annotations__ = BaseWeightsBasedEstimator.getWeights.__annotations__
        __doc__ = BaseWeightsBasedEstimator_multivariate.getWeights.__doc__
        
        if not self.fitted:
            raise NotFittedError("This LevelSetKDEx instance is not fitted yet. Call 'fit' with "
                                 "appropriate arguments before trying to compute weights.")
        
        #---
        
        yPred = self.estimator.predict(X).astype(np.float32)
        
        if len(yPred.shape) == 1:
            yPred = yPred.reshape(-1, 1)
            
        #---
        
        if self.equalBins:
            binPerPred = self._getEqualSizedClusters(y = yPred)
            
        else:
            binPerPred = self.kmeans.query(yPred)[1]
        
        #---
        
        neighborsList = [self.indicesPerBin[binIndex] for binIndex in binPerPred]
        
        weightsDataList = [(np.repeat(1 / len(neighbors), len(neighbors)), np.array(neighbors)) for neighbors in neighborsList]
        
        weightsDataList = restructureWeightsDataList_multivariate(weightsDataList = weightsDataList, 
                                                                  outputType = outputType, 
                                                                  y = self.yTrain,
                                                                  scalingList = scalingList,
                                                                  equalWeights = True)
        
        return weightsDataList
    
