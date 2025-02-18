import arviz as az
import numpy as np
import pymc as pm
import pytest
from pymc.distributions.censored import CensoredRV
from scipy import stats

from pymc_marketing.clv import ShiftedBetaGeoModelIndividual


class TestShiftedBetaGeoModel:
    @classmethod
    def setup_class(cls):
        def churned_data_from_percentage_alive(percentage_alive, initial_customers):
            n_alive = (np.asarray(percentage_alive) / 100 * initial_customers).astype(
                int
            )

            churned_at = np.zeros((initial_customers,), dtype=int)
            counter = 0
            for t, diff in enumerate((n_alive[:-1] - n_alive[1:]), start=1):
                churned_at[counter : counter + diff] = t
                counter += diff

            censoring_t = t + 1
            churned_at[counter:] = censoring_t

            return churned_at

        # Regular dataset from Fader, P. S., & Hardie, B. G. (2007). How to project customer retention.
        # Journal of Interactive Marketing, 21(1), 76-90. https://journals.sagepub.com/doi/pdf/10.1002/dir.20074
        cls.N = 1000
        cls.T = 8
        cls.customer_id = np.arange(cls.N)
        cls.churn_time = churned_data_from_percentage_alive(
            percentage_alive=[100.0, 63.1, 46.8, 38.2, 32.6, 28.9, 26.2, 24.1],
            initial_customers=cls.N,
        )
        cls.ref_MLE_estimates = {"alpha": 0.688, "beta": 1.182}

    @pytest.mark.parametrize("alpha_prior", (None, pm.HalfNormal.dist(sigma=10)))
    @pytest.mark.parametrize("beta_prior", (None, pm.HalfStudentT.dist(nu=4, sigma=10)))
    def test_model(self, alpha_prior, beta_prior):
        model = ShiftedBetaGeoModelIndividual(
            customer_id=self.customer_id,
            t_churn=self.churn_time,
            T=self.T,
            alpha_prior=alpha_prior,
            beta_prior=beta_prior,
        )

        assert isinstance(
            model.model["alpha"].owner.op,
            pm.HalfFlat if alpha_prior is None else pm.HalfNormal,
        )
        assert isinstance(
            model.model["beta"].owner.op,
            pm.HalfFlat if beta_prior is None else pm.HalfStudentT,
        )

        assert isinstance(model.model["theta"].owner.op, pm.Beta)
        assert isinstance(model.model["churn_censored"].owner.op, CensoredRV)
        assert isinstance(
            model.model["churn_censored"].owner.inputs[0].owner.op, pm.Geometric
        )

        assert model.model.eval_rv_shapes() == {
            "alpha": (),
            "alpha_log__": (),
            "beta": (),
            "beta_log__": (),
            "theta": (self.N,),
            "theta_logodds__": (self.N,),
        }
        assert model.model.coords == {
            "customer_id": tuple(range(self.N)),
        }

    def test_invalid_t_churn(self):
        match_msg = "t_churn must respect 0 < t_churn <= T"
        customer_id = range(3)

        with pytest.raises(ValueError, match=match_msg):
            ShiftedBetaGeoModelIndividual(
                customer_id=customer_id,
                t_churn=[10, 10, np.nan],
                T=10,
            )

        with pytest.raises(ValueError, match=match_msg):
            ShiftedBetaGeoModelIndividual(
                customer_id=customer_id,
                t_churn=[10, 10, 11],
                T=10,
            )

        with pytest.raises(ValueError, match=match_msg):
            ShiftedBetaGeoModelIndividual(
                customer_id=customer_id,
                t_churn=[-1, 8, 9],
                T=[8, 9, 10],
            )

    def test_model_repr(self):
        model = ShiftedBetaGeoModelIndividual(
            customer_id=self.customer_id,
            t_churn=self.churn_time,
            T=self.T,
            alpha_prior=pm.HalfNormal.dist(10),
        )

        assert model.__repr__().replace(" ", "") == (
            "Shifted-Beta-GeometricModel(IndividualCustomers)"
            "\nalpha~N**+(0,10)"
            "\nbeta~HalfFlat()"
            "\ntheta~Beta(alpha,beta)"
            f"\nchurn_censored~Censored(Geom(theta),-inf,{self.T})"
        )

    @pytest.mark.slow
    def test_model_convergence(self):
        model = ShiftedBetaGeoModelIndividual(
            customer_id=self.customer_id,
            t_churn=self.churn_time,
            T=self.T,
        )
        model.fit(chains=2, progressbar=False, random_seed=100)
        fit = model.fit_result.posterior
        np.testing.assert_allclose(
            [fit["alpha"].mean(), fit["beta"].mean()],
            [self.ref_MLE_estimates["alpha"], self.ref_MLE_estimates["beta"]],
            rtol=0.1,
        )

    def test_distribution_customer_churn_time(self):
        model = ShiftedBetaGeoModelIndividual(
            customer_id=[1, 2, 3],
            t_churn=np.array([10, 10, 10]),
            T=10,
        )

        customer_thetas = np.array([0.1, 0.5, 0.9])
        model._fit_result = az.from_dict(
            {
                "alpha": np.ones((2, 500)),  # Two chains, 500 draws each
                "beta": np.ones((2, 500)),
                "theta": np.full((2, 500, 3), customer_thetas),
            }
        )

        res = model.distribution_customer_churn_time(
            customer_id=[0, 1, 2], random_seed=116
        )
        np.testing.assert_allclose(
            res.mean(("chain", "draw")),
            stats.geom(customer_thetas).mean(),
            rtol=0.05,
        )

    def test_distribution_new_customer(self):
        model = ShiftedBetaGeoModelIndividual(
            customer_id=[1],
            t_churn=np.array([10]),
            T=10,
        )

        # theta ~ beta(7000, 3000) ~ 0.7
        model._fit_result = az.from_dict(
            {
                "alpha": np.full((2, 500), 7000),  # Two chains, 500 draws each
                "beta": np.full((2, 500), 3000),
            }
        )

        res = model.distribution_new_customer_theta(random_seed=141)
        np.testing.assert_allclose(res.mean(("chain", "draw")), 0.7, rtol=0.001)

        res = model.distribution_new_customer_churn_time(n=2, random_seed=146)
        np.testing.assert_allclose(
            res.mean(("chain", "draw", "new_customer_id")),
            stats.geom(0.7).mean(),
            rtol=0.05,
        )
